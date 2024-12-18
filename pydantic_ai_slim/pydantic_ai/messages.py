from __future__ import annotations as _annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Annotated, Any, Literal, Union

import pydantic
import pydantic_core
from typing_extensions import Self

from ._utils import now_utc as _now_utc


@dataclass
class SystemPromptPart:
    """A system prompt, generally written by the application developer.

    This gives the model context and guidance on how to respond.
    """

    content: str
    """The content of the prompt."""

    part_kind: Literal['system-prompt'] = 'system-prompt'
    """Part type identifier, this is available on all parts as a discriminator."""


@dataclass
class UserPromptPart:
    """A user prompt, generally written by the end user.

    Content comes from the `user_prompt` parameter of [`Agent.run`][pydantic_ai.Agent.run],
    [`Agent.run_sync`][pydantic_ai.Agent.run_sync], and [`Agent.run_stream`][pydantic_ai.Agent.run_stream].
    """

    content: str
    """The content of the prompt."""

    timestamp: datetime = field(default_factory=_now_utc)
    """The timestamp of the prompt."""

    part_kind: Literal['user-prompt'] = 'user-prompt'
    """Part type identifier, this is available on all parts as a discriminator."""


tool_return_ta: pydantic.TypeAdapter[Any] = pydantic.TypeAdapter(Any, config=pydantic.ConfigDict(defer_build=True))


@dataclass
class ToolReturnPart:
    """A tool return message, this encodes the result of running a tool."""

    tool_name: str
    """The name of the "tool" was called."""

    content: Any
    """The return value."""

    tool_call_id: str | None = None
    """Optional tool call identifier, this is used by some models including OpenAI."""

    timestamp: datetime = field(default_factory=_now_utc)
    """The timestamp, when the tool returned."""

    part_kind: Literal['tool-return'] = 'tool-return'
    """Part type identifier, this is available on all parts as a discriminator."""

    def model_response_str(self) -> str:
        if isinstance(self.content, str):
            return self.content
        else:
            return tool_return_ta.dump_json(self.content).decode()

    def model_response_object(self) -> dict[str, Any]:
        # gemini supports JSON dict return values, but no other JSON types, hence we wrap anything else in a dict
        if isinstance(self.content, dict):
            return tool_return_ta.dump_python(self.content, mode='json')  # pyright: ignore[reportUnknownMemberType]
        else:
            return {'return_value': tool_return_ta.dump_python(self.content, mode='json')}


error_details_ta = pydantic.TypeAdapter(list[pydantic_core.ErrorDetails], config=pydantic.ConfigDict(defer_build=True))


@dataclass
class RetryPromptPart:
    """A message back to a model asking it to try again.

    This can be sent for a number of reasons:

    * Pydantic validation of tool arguments failed, here content is derived from a Pydantic
      [`ValidationError`][pydantic_core.ValidationError]
    * a tool raised a [`ModelRetry`][pydantic_ai.exceptions.ModelRetry] exception
    * no tool was found for the tool name
    * the model returned plain text when a structured response was expected
    * Pydantic validation of a structured response failed, here content is derived from a Pydantic
      [`ValidationError`][pydantic_core.ValidationError]
    * a result validator raised a [`ModelRetry`][pydantic_ai.exceptions.ModelRetry] exception
    """

    content: list[pydantic_core.ErrorDetails] | str
    """Details of why and how the model should retry.

    If the retry was triggered by a [`ValidationError`][pydantic_core.ValidationError], this will be a list of
    error details.
    """

    tool_name: str | None = None
    """The name of the tool that was called, if any."""

    tool_call_id: str | None = None
    """Optional tool call identifier, this is used by some models including OpenAI."""

    timestamp: datetime = field(default_factory=_now_utc)
    """The timestamp, when the retry was triggered."""

    part_kind: Literal['retry-prompt'] = 'retry-prompt'
    """Part type identifier, this is available on all parts as a discriminator."""

    def model_response(self) -> str:
        if isinstance(self.content, str):
            description = self.content
        else:
            json_errors = error_details_ta.dump_json(self.content, exclude={'__all__': {'ctx'}}, indent=2)
            description = f'{len(self.content)} validation errors: {json_errors.decode()}'
        return f'{description}\n\nFix the errors and try again.'


ModelRequestPart = Annotated[
    Union[SystemPromptPart, UserPromptPart, ToolReturnPart, RetryPromptPart], pydantic.Discriminator('part_kind')
]
"""A message part sent by PydanticAI to a model."""


@dataclass
class ModelRequest:
    """A request generated by PydanticAI and sent to a model, e.g. a message from the PydanticAI app to the model."""

    parts: list[ModelRequestPart]
    """The parts of the user message."""

    kind: Literal['request'] = 'request'
    """Message type identifier, this is available on all parts as a discriminator."""


@dataclass
class TextPart:
    """A plain text response from a model."""

    content: str
    """The text content of the response."""

    part_kind: Literal['text'] = 'text'
    """Part type identifier, this is available on all parts as a discriminator."""


@dataclass
class ArgsJson:
    """Tool arguments as a JSON string."""

    args_json: str
    """A JSON string of arguments."""


@dataclass
class ArgsDict:
    """Tool arguments as a Python dictionary."""

    args_dict: dict[str, Any]
    """A python dictionary of arguments."""


@dataclass
class ToolCallPart:
    """A tool call from a model."""

    tool_name: str
    """The name of the tool to call."""

    args: ArgsJson | ArgsDict
    """The arguments to pass to the tool.

    Either as JSON or a Python dictionary depending on how data was returned.
    """

    tool_call_id: str | None = None
    """Optional tool call identifier, this is used by some models including OpenAI."""

    part_kind: Literal['tool-call'] = 'tool-call'
    """Part type identifier, this is available on all parts as a discriminator."""

    @classmethod
    def from_json(cls, tool_name: str, args_json: str, tool_call_id: str | None = None) -> Self:
        return cls(tool_name, ArgsJson(args_json), tool_call_id)

    @classmethod
    def from_dict(cls, tool_name: str, args_dict: dict[str, Any], tool_call_id: str | None = None) -> Self:
        return cls(tool_name, ArgsDict(args_dict), tool_call_id)

    def has_content(self) -> bool:
        if isinstance(self.args, ArgsDict):
            return any(self.args.args_dict.values())
        else:
            return bool(self.args.args_json)


ModelResponsePart = Annotated[Union[TextPart, ToolCallPart], pydantic.Discriminator('part_kind')]
"""A message part returned by a model."""


@dataclass
class ModelResponse:
    """A response from a model, e.g. a message from the model to the PydanticAI app."""

    parts: list[ModelResponsePart]
    """The parts of the model message."""

    timestamp: datetime = field(default_factory=_now_utc)
    """The timestamp of the response.

    If the model provides a timestamp in the response (as OpenAI does) that will be used.
    """

    kind: Literal['response'] = 'response'
    """Message type identifier, this is available on all parts as a discriminator."""

    @classmethod
    def from_text(cls, content: str, timestamp: datetime | None = None) -> Self:
        return cls([TextPart(content)], timestamp=timestamp or _now_utc())

    @classmethod
    def from_tool_call(cls, tool_call: ToolCallPart) -> Self:
        return cls([tool_call])


ModelMessage = Annotated[Union[ModelRequest, ModelResponse], pydantic.Discriminator('kind')]
"""Any message send to or returned by a model."""

ModelMessagesTypeAdapter = pydantic.TypeAdapter(list[ModelMessage], config=pydantic.ConfigDict(defer_build=True))
"""Pydantic [`TypeAdapter`][pydantic.type_adapter.TypeAdapter] for (de)serializing messages."""


@dataclass
class TextPartDelta:
    content_delta: str
    part_delta_kind: Literal['text'] = 'text'

    def apply(self, part: TextPart) -> TextPart:
        return replace(part, content=part.content + self.content_delta)


@dataclass
class ToolCallPartDelta:
    args_json_delta: str
    part_delta_kind: Literal['tool_call'] = 'tool_call'

    def apply(self, part: ToolCallPart) -> ToolCallPart:
        assert isinstance(part.args, ArgsJson), 'Cannot apply deltas to non-JSON tool arguments'
        updated_json = part.args.args_json + self.args_json_delta
        return replace(part, args=ArgsJson(updated_json))


ModelResponsePartDelta = Annotated[Union[TextPartDelta, ToolCallPartDelta], pydantic.Discriminator('part_delta_kind')]


@dataclass
class PartStartEvent:
    """If multiple PartStartEvents are received with the same index, the new one should fully replace the old one."""

    index: int
    part: ModelResponsePart
    event_kind: Literal['part_start'] = 'part_start'


@dataclass
class PartDeltaEvent:
    index: int
    delta: ModelResponsePartDelta
    event_kind: Literal['part_delta'] = 'part_delta'


@dataclass
class PartStopEvent:
    index: int
    part: ModelResponsePart
    event_kind: Literal['part_stop'] = 'part_stop'


ModelResponseStreamEvent = Annotated[
    Union[PartStartEvent, PartDeltaEvent, PartStopEvent], pydantic.Discriminator('event_kind')
]
