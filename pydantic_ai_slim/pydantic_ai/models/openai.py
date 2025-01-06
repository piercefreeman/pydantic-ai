from __future__ import annotations as _annotations

from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import chain
from typing import Literal, Union, overload

from httpx import AsyncClient as AsyncHTTPClient
from typing_extensions import assert_never

from .. import UnexpectedModelBehavior, _utils, result
from .._utils import PeekableAsyncStream, guard_tool_call_id as _guard_tool_call_id, now_utc as _now_utc
from ..messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponsePart,
    ModelResponseStreamEvent,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
    ToolReturnPart,
    UserPromptPart,
)
from ..result import Usage
from ..settings import ModelSettings
from ..tools import ToolDefinition
from . import (
    AgentModel,
    Model,
    StreamedResponse,
    cached_async_http_client,
    check_allow_model_requests,
)

try:
    from openai import NOT_GIVEN, AsyncOpenAI, AsyncStream
    from openai.types import ChatModel, chat
    from openai.types.chat import ChatCompletionChunk
    from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall
except ImportError as _import_error:
    raise ImportError(
        'Please install `openai` to use the OpenAI model, '
        "you can use the `openai` optional group — `pip install 'pydantic-ai-slim[openai]'`"
    ) from _import_error

OpenAIModelName = Union[ChatModel, str]
"""
Using this more broad type for the model name instead of the ChatModel definition
allows this model to be used more easily with other model types (ie, Ollama)
"""


@dataclass(init=False)
class OpenAIModel(Model):
    """A model that uses the OpenAI API.

    Internally, this uses the [OpenAI Python client](https://github.com/openai/openai-python) to interact with the API.

    Apart from `__init__`, all methods are private or match those of the base class.
    """

    model_name: OpenAIModelName
    client: AsyncOpenAI = field(repr=False)

    def __init__(
        self,
        model_name: OpenAIModelName,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        openai_client: AsyncOpenAI | None = None,
        http_client: AsyncHTTPClient | None = None,
    ):
        """Initialize an OpenAI model.

        Args:
            model_name: The name of the OpenAI model to use. List of model names available
                [here](https://github.com/openai/openai-python/blob/v1.54.3/src/openai/types/chat_model.py#L7)
                (Unfortunately, despite being ask to do so, OpenAI do not provide `.inv` files for their API).
            base_url: The base url for the OpenAI requests. If not provided, the `OPENAI_BASE_URL` environment variable
                will be used if available. Otherwise, defaults to OpenAI's base url.
            api_key: The API key to use for authentication, if not provided, the `OPENAI_API_KEY` environment variable
                will be used if available.
            openai_client: An existing
                [`AsyncOpenAI`](https://github.com/openai/openai-python?tab=readme-ov-file#async-usage)
                client to use. If provided, `base_url`, `api_key`, and `http_client` must be `None`.
            http_client: An existing `httpx.AsyncClient` to use for making HTTP requests.
        """
        self.model_name: OpenAIModelName = model_name
        if openai_client is not None:
            assert http_client is None, 'Cannot provide both `openai_client` and `http_client`'
            assert base_url is None, 'Cannot provide both `openai_client` and `base_url`'
            assert api_key is None, 'Cannot provide both `openai_client` and `api_key`'
            self.client = openai_client
        elif http_client is not None:
            self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, http_client=http_client)
        else:
            self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, http_client=cached_async_http_client())

    async def agent_model(
        self,
        *,
        function_tools: list[ToolDefinition],
        allow_text_result: bool,
        result_tools: list[ToolDefinition],
    ) -> AgentModel:
        check_allow_model_requests()
        tools = [self._map_tool_definition(r) for r in function_tools]
        if result_tools:
            tools += [self._map_tool_definition(r) for r in result_tools]
        return OpenAIAgentModel(
            self.client,
            self.model_name,
            allow_text_result,
            tools,
        )

    def name(self) -> str:
        return f'openai:{self.model_name}'

    @staticmethod
    def _map_tool_definition(f: ToolDefinition) -> chat.ChatCompletionToolParam:
        return {
            'type': 'function',
            'function': {
                'name': f.name,
                'description': f.description,
                'parameters': f.parameters_json_schema,
            },
        }


@dataclass
class OpenAIAgentModel(AgentModel):
    """Implementation of `AgentModel` for OpenAI models."""

    client: AsyncOpenAI
    model_name: OpenAIModelName
    allow_text_result: bool
    tools: list[chat.ChatCompletionToolParam]

    async def request(
        self, messages: list[ModelMessage], model_settings: ModelSettings | None
    ) -> tuple[ModelResponse, result.Usage]:
        response = await self._completions_create(messages, False, model_settings)
        return self._process_response(response), _map_usage(response)

    @asynccontextmanager
    async def request_stream(
        self, messages: list[ModelMessage], model_settings: ModelSettings | None
    ) -> AsyncIterator[StreamedResponse]:
        response = await self._completions_create(messages, True, model_settings)
        async with response:
            yield await self._process_streamed_response(response)

    @overload
    async def _completions_create(
        self, messages: list[ModelMessage], stream: Literal[True], model_settings: ModelSettings | None
    ) -> AsyncStream[ChatCompletionChunk]:
        pass

    @overload
    async def _completions_create(
        self, messages: list[ModelMessage], stream: Literal[False], model_settings: ModelSettings | None
    ) -> chat.ChatCompletion:
        pass

    async def _completions_create(
        self, messages: list[ModelMessage], stream: bool, model_settings: ModelSettings | None
    ) -> chat.ChatCompletion | AsyncStream[ChatCompletionChunk]:
        # standalone function to make it easier to override
        if not self.tools:
            tool_choice: Literal['none', 'required', 'auto'] | None = None
        elif not self.allow_text_result:
            tool_choice = 'required'
        else:
            tool_choice = 'auto'

        openai_messages = list(chain(*(self._map_message(m) for m in messages)))

        model_settings = model_settings or {}

        return await self.client.chat.completions.create(
            model=self.model_name,
            messages=openai_messages,
            n=1,
            parallel_tool_calls=True if self.tools else NOT_GIVEN,
            tools=self.tools or NOT_GIVEN,
            tool_choice=tool_choice or NOT_GIVEN,
            stream=stream,
            stream_options={'include_usage': True} if stream else NOT_GIVEN,
            max_tokens=model_settings.get('max_tokens', NOT_GIVEN),
            temperature=model_settings.get('temperature', NOT_GIVEN),
            top_p=model_settings.get('top_p', NOT_GIVEN),
            timeout=model_settings.get('timeout', NOT_GIVEN),
        )

    @staticmethod
    def _process_response(response: chat.ChatCompletion) -> ModelResponse:
        """Process a non-streamed response, and prepare a message to return."""
        timestamp = datetime.fromtimestamp(response.created, tz=timezone.utc)
        choice = response.choices[0]
        items: list[ModelResponsePart] = []
        if choice.message.content is not None:
            items.append(TextPart(choice.message.content))
        if choice.message.tool_calls is not None:
            for c in choice.message.tool_calls:
                items.append(ToolCallPart.from_raw_args(c.function.name, c.function.arguments, c.id))
        return ModelResponse(items, timestamp=timestamp)

    @staticmethod
    async def _process_streamed_response(response: AsyncStream[ChatCompletionChunk]) -> OpenAIStreamedResponse:
        """Process a streamed response, and prepare a streaming response to return."""
        peekable_response = PeekableAsyncStream(response)
        first_chunk = await peekable_response.peek()
        if first_chunk is None:
            raise UnexpectedModelBehavior('Streamed response ended without content or tool calls')

        return OpenAIStreamedResponse(peekable_response, datetime.fromtimestamp(first_chunk.created, tz=timezone.utc))

    @classmethod
    def _map_message(cls, message: ModelMessage) -> Iterable[chat.ChatCompletionMessageParam]:
        """Just maps a `pydantic_ai.Message` to a `openai.types.ChatCompletionMessageParam`."""
        if isinstance(message, ModelRequest):
            yield from cls._map_user_message(message)
        elif isinstance(message, ModelResponse):
            texts: list[str] = []
            tool_calls: list[chat.ChatCompletionMessageToolCallParam] = []
            for item in message.parts:
                if isinstance(item, TextPart):
                    texts.append(item.content)
                elif isinstance(item, ToolCallPart):
                    tool_calls.append(_map_tool_call(item))
                else:
                    assert_never(item)
            message_param = chat.ChatCompletionAssistantMessageParam(role='assistant')
            if texts:
                # Note: model responses from this model should only have one text item, so the following
                # shouldn't merge multiple texts into one unless you switch models between runs:
                message_param['content'] = '\n\n'.join(texts)
            if tool_calls:
                message_param['tool_calls'] = tool_calls
            yield message_param
        else:
            assert_never(message)

    @classmethod
    def _map_user_message(cls, message: ModelRequest) -> Iterable[chat.ChatCompletionMessageParam]:
        for part in message.parts:
            if isinstance(part, SystemPromptPart):
                yield chat.ChatCompletionSystemMessageParam(role='system', content=part.content)
            elif isinstance(part, UserPromptPart):
                yield chat.ChatCompletionUserMessageParam(role='user', content=part.content)
            elif isinstance(part, ToolReturnPart):
                yield chat.ChatCompletionToolMessageParam(
                    role='tool',
                    tool_call_id=_guard_tool_call_id(t=part, model_source='OpenAI'),
                    content=part.model_response_str(),
                )
            elif isinstance(part, RetryPromptPart):
                if part.tool_name is None:
                    yield chat.ChatCompletionUserMessageParam(role='user', content=part.model_response())
                else:
                    yield chat.ChatCompletionToolMessageParam(
                        role='tool',
                        tool_call_id=_guard_tool_call_id(t=part, model_source='OpenAI'),
                        content=part.model_response(),
                    )
            else:
                assert_never(part)


_CONTENT_INDEX = 0


@dataclass
class OpenAIStreamedResponse(StreamedResponse):
    """Implementation of `StreamedResponse` for OpenAI models."""

    _response: AsyncIterable[ChatCompletionChunk]
    _timestamp: datetime

    _usage: result.Usage = field(default_factory=result.Usage, init=False)

    _delta_tool_calls: dict[int, ChoiceDeltaToolCall] = field(default_factory=dict, init=False)
    _content_part: TextPart | None = field(default=None, init=False)
    _tool_call_parts: dict[int, ToolCallPart] = field(default_factory=dict, init=False)
    _async_iterator: AsyncIterator[ModelResponseStreamEvent | None] | None = field(default=None, init=False)

    def __aiter__(self) -> AsyncIterator[ModelResponseStreamEvent | None]:
        if self._async_iterator is None:
            self._async_iterator = self._get_async_iterator()
        return self._async_iterator

    async def __anext__(self) -> ModelResponseStreamEvent | None:
        if self._async_iterator is None:
            self._async_iterator = self._get_async_iterator()
        return await self._async_iterator.__anext__()

    async def _get_async_iterator(self) -> AsyncIterator[ModelResponseStreamEvent | None]:
        async for chunk in self._response:
            if self._timestamp is None:
                self._timestamp = datetime.fromtimestamp(chunk.created, tz=timezone.utc)

            self._usage += _map_usage(chunk)
            try:
                choice = chunk.choices[0]
            except IndexError:
                continue

            for e in self._update_parts_for_content_delta(choice.delta.content):
                yield e

            for new in choice.delta.tool_calls or []:
                if current := self._delta_tool_calls.get(new.index):
                    if new.function is not None:
                        if current.function is None:
                            replace_existing_part = True
                            current.function = new.function
                        else:
                            replace_existing_part = bool(new.function.name)
                            current.function.name = _utils.add_optional(current.function.name, new.function.name)
                            current.function.arguments = _utils.add_optional(
                                current.function.arguments, new.function.arguments
                            )
                        for e in self._update_parts_for_tool_call_delta(new, replace_existing_part):
                            yield e
                else:
                    self._delta_tool_calls[new.index] = new
                    for e in self._update_parts_for_tool_call_delta(new, True):
                        yield e

    def get(self, *, final: bool = False) -> ModelResponse:
        items: list[ModelResponsePart] = [self._content_part] if self._content_part is not None else []
        items.extend([self._tool_call_parts[k] for k in sorted(self._tool_call_parts.keys())])
        return ModelResponse(items, timestamp=self.timestamp())

    def usage(self) -> Usage:
        return self._usage

    def timestamp(self) -> datetime:
        return self._timestamp or _now_utc()

    def _update_parts_for_content_delta(self, choice_delta_content: str | None) -> Iterator[ModelResponseStreamEvent]:
        if choice_delta_content is None:
            return

        existing_content = self._content_part
        if existing_content is None:
            part = TextPart(content=choice_delta_content)
            self._content_part = part
            yield PartStartEvent(index=_CONTENT_INDEX, part=part)
        else:
            delta = TextPartDelta(content_delta=choice_delta_content)
            self._content_part = delta.apply(existing_content)
            yield PartDeltaEvent(index=_CONTENT_INDEX, delta=delta)

    def _update_parts_for_tool_call_delta(
        self, tc: ChoiceDeltaToolCall, replace: bool
    ) -> Iterator[ModelResponseStreamEvent]:
        if tc.function is None:
            return

        if replace:
            assert tc.function.name is not None
            new_part = ToolCallPart.from_raw_args(tc.function.name, tc.function.arguments or '', tc.id)
            self._tool_call_parts[tc.index] = new_part
            yield PartStartEvent(index=tc.index, part=new_part)
        else:
            assert (existing_part := self._tool_call_parts.get(tc.index)) is not None
            delta = ToolCallPartDelta(args_json_delta=tc.function.arguments or '')
            self._tool_call_parts[tc.index] = delta.apply(existing_part)
            yield PartDeltaEvent(index=tc.index, delta=delta)


def _map_tool_call(t: ToolCallPart) -> chat.ChatCompletionMessageToolCallParam:
    return chat.ChatCompletionMessageToolCallParam(
        id=_guard_tool_call_id(t=t, model_source='OpenAI'),
        type='function',
        function={'name': t.tool_name, 'arguments': t.args_as_json_str()},
    )


def _map_usage(response: chat.ChatCompletion | ChatCompletionChunk) -> result.Usage:
    usage = response.usage
    if usage is None:
        return result.Usage()
    else:
        details: dict[str, int] = {}
        if usage.completion_tokens_details is not None:
            details.update(usage.completion_tokens_details.model_dump(exclude_none=True))
        if usage.prompt_tokens_details is not None:
            details.update(usage.prompt_tokens_details.model_dump(exclude_none=True))
        return result.Usage(
            request_tokens=usage.prompt_tokens,
            response_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            details=details,
        )
