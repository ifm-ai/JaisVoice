import json
import pytest

from k2_runtime.protocol import K2Parser, normalize_messages, tool_call_deltas

TOOLS = [{"type": "function", "function": {"name": "browser_navigate", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}}}]


def test_thought_and_unicode_markers_split_at_every_character():
    parser = K2Parser()
    events = []
    for char in 'private reasoning</ifm|think_faster>مرحباً بالعالم':
        events += parser.feed(char)
    assert ''.join(event.get('content', '') for event in events) == 'مرحباً بالعالم'
    assert parser.reasoning == 'private reasoning'
    assert parser.finish('stop') == 'stop'


@pytest.mark.parametrize('effort,marker', [('medium', '</ifm|think_fast>'), ('high', '</ifm|think>')])
def test_medium_high_reasoning_stays_separate(effort, marker):
    parser = K2Parser(reasoning_effort=effort)
    events = []
    for character in 'private thought' + marker + 'Final answer':
        events.extend(parser.feed(character))
    assert parser.reasoning == 'private thought'
    assert ''.join(event.get('content', '') for event in events) == 'Final answer'
    assert parser.finish('stop') == 'stop'


def test_real_k2_generic_close_alias_after_medium_opening():
    parser = K2Parser(reasoning_effort='medium')
    events = []
    for character in 'private thought</ifm|think>Final answer':
        events.extend(parser.feed(character))
    assert parser.reasoning == 'private thought'
    assert ''.join(event.get('content', '') for event in events) == 'Final answer'
    assert parser.finish('stop') == 'stop'


def test_tool_block_remains_structured_and_out_of_text():
    parser = K2Parser(TOOLS)
    text = 'thought</ifm|think_faster><ifm|tool_calls><ifm|tool_call>{"name":"browser_navigate","arguments":{"url":"https://example.com"}}</ifm|tool_call></ifm|tool_calls>'
    events = []
    for char in text:
        events += parser.feed(char)
    assert parser.content == ''
    assert parser.finish('stop') == 'tool_calls'
    assert json.loads(parser.calls[0]['function']['arguments']) == {'url': 'https://example.com'}
    assert len([e for e in events if 'tool_calls' in e]) == 1


@pytest.mark.parametrize('text', ['no final answer', 'thought</ifm|think_faster><ifm|tool_calls>', 'thought</ifm|think_faster><ifm|tool_'])
def test_incomplete_output_fails(text):
    parser = K2Parser(TOOLS)
    parser.feed(text)
    with pytest.raises(ValueError):
        parser.finish('stop')


def test_length_is_error_even_if_content_exists():
    parser = K2Parser()
    parser.feed('thinking</ifm|think_faster>some content')
    with pytest.raises(ValueError, match='token limit'):
        parser.finish('length')


def test_tool_roundtrip_message_normalization_without_mutation():
    history = [{'role': 'assistant', 'content': None, 'tool_calls': [{'type': 'function', 'function': {'name': 'browser_navigate', 'arguments': '{"url":"https://example.com"}'}}]}, {'role': 'tool', 'content': 'Page title: Example'}]
    result = normalize_messages(history)
    assert result[0]['think_faster'] == ''
    assert result[0]['tool_calls'][0]['function']['arguments'] == {'url': 'https://example.com'}
    assert isinstance(history[0]['tool_calls'][0]['function']['arguments'], str)


def test_unknown_tool_rejected():
    parser = K2Parser(TOOLS)
    with pytest.raises(ValueError, match='unknown tool'):
        parser.feed('thought</ifm|think_faster><ifm|tool_calls><ifm|tool_call>{"name":"not_allowed","arguments":{}}</ifm|tool_call></ifm|tool_calls>')


def test_three_tool_calls_emitted_one_per_delta():
    parser = K2Parser(TOOLS)
    block = ''.join('<ifm|tool_call>' + json.dumps({'name': 'browser_navigate', 'arguments': {'url': f'https://example.com/{i}'}}) + '</ifm|tool_call>' for i in range(3))
    parser.feed('thought</ifm|think_faster><ifm|tool_calls>' + block + '</ifm|tool_calls>')
    assert parser.finish('stop') == 'tool_calls'
    deltas = tool_call_deltas(parser.calls)
    assert len(deltas) == 3
    assert all(len(delta['tool_calls']) == 1 for delta in deltas)
    assert [delta['tool_calls'][0]['index'] for delta in deltas] == [0, 1, 2]


def test_literal_control_characters_inside_tool_string_are_reencoded():
    parser = K2Parser(TOOLS)
    parser.feed('thought</ifm|think_faster><ifm|tool_calls><ifm|tool_call>{"name":"browser_navigate","arguments":{"url":"first\nsecond\tthird"}}</ifm|tool_call></ifm|tool_calls>')
    assert parser.finish('stop') == 'tool_calls'
    serialized = parser.calls[0]['function']['arguments']
    assert '\n' not in serialized
    assert json.loads(serialized)['url'] == 'first\nsecond\tthird'


def test_unterminated_json_still_fails_after_control_character_support():
    parser = K2Parser(TOOLS)
    with pytest.raises(ValueError):
        parser.feed('thought</ifm|think_faster><ifm|tool_calls><ifm|tool_call>{"name":"browser_navigate","arguments":{"url":"unterminated}}</ifm|tool_call></ifm|tool_calls>')


def test_native_xml_tool_call_and_schema_validation():
    tools = [{'type': 'function', 'function': {'name': 'fill', 'parameters': {'type': 'object', 'properties': {'text': {'type': 'string'}, 'count': {'type': 'integer'}, 'enabled': {'type': 'boolean'}, 'items': {'type': 'array', 'items': {'type': 'string'}}}, 'required': ['text', 'count']}}}]
    parser = K2Parser(tools)
    call = parser.parse_call('fill\n<ifm|arg_key>text</ifm|arg_key><ifm|arg_value>Hello\nworld</ifm|arg_value><ifm|arg_key>count</ifm|arg_key><ifm|arg_value>3</ifm|arg_value><ifm|arg_key>enabled</ifm|arg_key><ifm|arg_value>false</ifm|arg_value><ifm|arg_key>items</ifm|arg_key><ifm|arg_value>["a", "b"]</ifm|arg_value>')
    assert call['arguments'] == {'text': 'Hello\nworld', 'count': 3, 'enabled': False, 'items': ['a', 'b']}
    with pytest.raises(Exception):
        parser.parse_call('fill\n<ifm|arg_key>count</ifm|arg_key><ifm|arg_value>"not-an-integer"</ifm|arg_value>')


def test_native_xml_incomplete_markup_is_not_repaired():
    parser = K2Parser(TOOLS)
    with pytest.raises(ValueError, match='Malformed'):
        parser.parse_call('browser_navigate\n<ifm|arg_key>url</ifm|arg_key><ifm|arg_value>https://example.com')
