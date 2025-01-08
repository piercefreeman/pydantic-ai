from __future__ import annotations as _annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Annotated, Any, Literal, Union, cast, overload

import pydantic
import pydantic_core
from typing_extensions import Self, assert_never

from ._utils import now_utc as _now_utc


@dataclass
class SystemPromptPart:
    """A system prompt, generally written by the application developer.

    This gives the model context and guidance on how to respond.
    """

    content: str
    """The content of the prompt."""

    dynamic_ref: str | None = None
    """The ref of the dynamic system prompt function that generated this part.

    Only set if system prompt is dynamic, see [`system_prompt`][pydantic_ai.Agent.system_prompt] for more information.
    """

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

    def has_content(self) -> bool:
        return bool(self.content)


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
    def from_raw_args(cls, tool_name: str, args: str | dict[str, Any], tool_call_id: str | None = None) -> Self:
        """Create a `ToolCallPart` from raw arguments."""
        if isinstance(args, str):
            return cls(tool_name, ArgsJson(args), tool_call_id)
        elif isinstance(args, dict):
            return cls(tool_name, ArgsDict(args), tool_call_id)
        else:
            assert_never(args)

    def args_as_dict(self) -> dict[str, Any]:
        """Return the arguments as a Python dictionary.

        This is just for convenience with models that require dicts as input.
        """
        if isinstance(self.args, ArgsDict):
            return self.args.args_dict
        args = pydantic_core.from_json(self.args.args_json)
        assert isinstance(args, dict), 'args should be a dict'
        return cast(dict[str, Any], args)

    def args_as_json_str(self) -> str:
        """Return the arguments as a JSON string.

        This is just for convenience with models that require JSON strings as input.
        """
        if isinstance(self.args, ArgsJson):
            return self.args.args_json
        return pydantic_core.to_json(self.args.args_dict).decode()

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
        return cls([TextPart(content=content)], timestamp=timestamp or _now_utc())

    @classmethod
    def from_tool_call(cls, tool_call: ToolCallPart) -> Self:
        return cls([tool_call])


ModelMessage = Annotated[Union[ModelRequest, ModelResponse], pydantic.Discriminator('kind')]
"""Any message send to or returned by a model."""

ModelMessagesTypeAdapter = pydantic.TypeAdapter(list[ModelMessage], config=pydantic.ConfigDict(defer_build=True))
"""Pydantic [`TypeAdapter`][pydantic.type_adapter.TypeAdapter] for (de)serializing messages."""


@dataclass
class TextPartDelta:
    """A text part delta."""

    content_delta: str

    part_delta_kind: Literal['text'] = 'text'

    def apply(self, part: ModelResponsePart) -> TextPart:
        if not isinstance(part, TextPart):
            raise ValueError('Cannot apply TextPartDeltas to non-TextParts')
        return replace(part, content=part.content + self.content_delta)


@dataclass
class ToolCallPartDelta:
    """A tool call part delta."""

    tool_name_delta: str | None = None
    args_delta: str | dict[str, Any] | None = None
    tool_call_id: str | None = None

    part_delta_kind: Literal['tool_call'] = 'tool_call'

    def as_part(self) -> ToolCallPart | None:
        """Converts to a ToolCallPart if the required information is present, otherwise returns None."""
        if self.tool_name_delta is None or self.args_delta is None:
            return None

        return ToolCallPart.from_raw_args(
            self.tool_name_delta,
            self.args_delta,
            self.tool_call_id,
        )

    @overload
    def apply(self, part: ModelResponsePart) -> ToolCallPart: ...

    @overload
    def apply(self, part: ModelResponsePart | ToolCallPartDelta) -> ToolCallPart | ToolCallPartDelta: ...

    def apply(self, part: ModelResponsePart | ToolCallPartDelta) -> ToolCallPart | ToolCallPartDelta:
        if isinstance(part, ToolCallPart):
            return self._apply_to_part(part)

        if isinstance(part, ToolCallPartDelta):
            return self._apply_to_delta(part)

        raise ValueError(f'Can only apply ToolCallPartDeltas to ToolCallParts or ToolCallPartDeltas, not {part}')

    def _apply_to_delta(self, delta: ToolCallPartDelta) -> ToolCallPart | ToolCallPartDelta:
        if self.tool_name_delta:
            # I'm not sure how common it is to have deltas on the tool name, but I've handled it here for completeness
            updated_tool_name_delta = (delta.tool_name_delta or '') + self.tool_name_delta
            delta = replace(delta, tool_name_delta=updated_tool_name_delta)

        if isinstance(self.args_delta, str):
            if isinstance(delta.args_delta, dict):
                raise NotImplementedError('Cannot apply a JSON args delta to a dict args delta')
            updated_args_delta = (delta.args_delta or '') + self.args_delta
            delta = replace(delta, args_delta=updated_args_delta)
        elif isinstance(self.args_delta, dict):
            if isinstance(delta.args_delta, str):
                raise NotImplementedError('Cannot apply a dict args delta to a JSON args delta')
            updated_args_delta = {**(delta.args_delta or {}), **self.args_delta}
            delta = replace(delta, args_delta=updated_args_delta)

        if self.tool_call_id:
            # Don't treat tool_call_id as a delta, just replace it
            if delta.tool_call_id is not None and delta.tool_call_id != self.tool_call_id:
                raise ValueError('Cannot apply a new tool_call_id to a ToolCallPartDelta that already has one')
            delta = replace(delta, tool_call_id=self.tool_call_id)

        # If we have enough data to create a full ToolCallPart, do so:
        if delta.tool_name_delta is not None and delta.args_delta is not None:
            return ToolCallPart.from_raw_args(
                delta.tool_name_delta,
                delta.args_delta,
                delta.tool_call_id,
            )

        return delta

    def _apply_to_part(self, part: ToolCallPart) -> ToolCallPart:
        if self.tool_name_delta:
            # I'm not sure how common it is to have deltas on the tool name, but I've handled it here for completeness
            tool_name = part.tool_name + self.tool_name_delta
            part = replace(part, tool_name=tool_name)

        if isinstance(self.args_delta, str):
            if not isinstance(part.args, ArgsJson):
                raise ValueError('Cannot apply deltas to non-JSON tool arguments')
            updated_json = part.args.args_json + self.args_delta
            part = replace(part, args=ArgsJson(updated_json))
        elif isinstance(self.args_delta, dict):
            if not isinstance(part.args, ArgsDict):
                raise ValueError('Cannot apply deltas to non-dict tool arguments')
            updated_dict = {**(part.args.args_dict or {}), **self.args_delta}
            part = replace(part, args=ArgsDict(updated_dict))

        if self.tool_call_id:
            # Don't treat tool_call_id as a delta, just replace it
            if part.tool_call_id is not None and part.tool_call_id != self.tool_call_id:
                raise ValueError('Cannot apply a new tool_call_id to a ToolCallPart that already has one')
            part = replace(part, tool_call_id=self.tool_call_id)
        return part


ModelResponsePartDelta = Annotated[Union[TextPartDelta, ToolCallPartDelta], pydantic.Discriminator('part_delta_kind')]


@dataclass
class PartStartEvent:
    """If multiple PartStartEvents are received with the same index, the new one should fully replace the old one."""

    index: int
    part: ModelResponsePart
    event_kind: Literal['part_start'] = 'part_start'


@dataclass
class PartDeltaEvent:
    """A part delta event."""

    index: int
    delta: ModelResponsePartDelta
    event_kind: Literal['part_delta'] = 'part_delta'


ModelResponseStreamEvent = Annotated[Union[PartStartEvent, PartDeltaEvent], pydantic.Discriminator('event_kind')]
