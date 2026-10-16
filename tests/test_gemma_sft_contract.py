from rca_lab.train.mcp_sft import IGNORE_INDEX, tokenize_turns


class GemmaBoundaryTokenizer:
    unk_token_id = 0

    def convert_tokens_to_ids(self, text):
        return 999 if text == '<|tool_response>' else 0

    def apply_chat_template(self, messages, *, tokenize, tools, add_generation_prompt=False, **kwargs):
        assert kwargs == {'enable_thinking': True, 'preserve_thinking': True}
        if add_generation_prompt:
            return [1, 2, 3]
        return [1, 2, 3, 44, 55, 999]


def test_gemma_supervises_reasoning_call_end_but_never_response_opener():
    messages = [{'role': 'user', 'content': 'u'}, {'role': 'assistant', 'reasoning_content': 'r', 'tool_calls': [{'function': {'name': 'x', 'arguments': {}}}]}]
    [example] = tokenize_turns(GemmaBoundaryTokenizer(), messages, [], supervise_end_token=True, chat_template_kwargs={'enable_thinking': True, 'preserve_thinking': True})
    assert example['input_ids'] == [1, 2, 3, 44, 55]
    assert example['labels'] == [IGNORE_INDEX] * 3 + [44, 55]


def test_fused_expert_parameter_adapters_reject_unsupported_dropout():
    import pytest

    from rca_lab.train.mcp_sft import LoraSpec
    with pytest.raises(ValueError, match="require dropout=0"):
        LoraSpec(target_parameters=["experts.gate_up_proj"], dropout=0.05)
    assert LoraSpec(target_parameters=["experts.gate_up_proj"], dropout=0).dropout == 0


class GemmaWeightedTokenizer:
    """Token structure from the pinned Gemma template, with small content IDs."""
    unk_token_id = 0
    chat_template = '<|channel>thought\n<channel|><|tool_call>call:x{}<tool_call|>'
    markers = {'<|channel>': 100, '<channel|>': 101, '<|tool_call>': 48,
               '<tool_call|>': 49, '<|tool_response>': 50}

    def convert_tokens_to_ids(self, text):
        return self.markers.get(text, 0)

    def encode(self, text, *, add_special_tokens):
        assert not add_special_tokens
        return {'<|channel>thought\n': [100, 300, 108], '<|channel>thought\n<channel|>': [100, 300, 108, 101]}[text]

    def apply_chat_template(self, messages, *, tokenize, tools, add_generation_prompt=False, **kwargs):
        prefix = [2, 105, len(messages) if add_generation_prompt else len(messages)-1, 107]
        if add_generation_prompt:
            return prefix
        m = messages[-1]
        suffix = [100, 300, 108, 700, 701, 108, 101] if m.get('reasoning_content') else []
        for _ in m.get('tool_calls', []):
            suffix += [48, 800, 801, 49]
        return prefix + suffix + ([50] if m.get('tool_calls') else [900, 106])


def weighted_messages(stage='first', reasoning='reason', calls=1):
    call = lambda name: {'id': name, 'type': 'function', 'function': {'name': name, 'arguments': '{}'}}
    messages = [{'role': 'user', 'content': 'investigate'}]
    if stage != 'first':
        messages += [{'role': 'assistant', 'reasoning_content': 'prior', 'tool_calls': [call('search')], 'supervise': False},
                     {'role': 'tool', 'tool_call_id': 'search', 'content': 'observed'}]
    messages += [{'role': 'assistant', 'reasoning_content': reasoning,
                  'tool_calls': [call('submit_rca' if stage == 'final' else f'lookup{i}') for i in range(calls)]}]
    return messages


def test_gemma_weighting_preserves_ids_labels_and_all_structural_tokens():
    from rca_lab.mcp.muse_grammar import MUSE_START_TOKEN_ID
    for stage in ['first', 'intermediate', 'final']:
        for calls in [1, 2]:
            kwargs = dict(supervise_end_token=True, call_header_id=MUSE_START_TOKEN_ID,
                          chat_template_kwargs={'enable_thinking': True})
            [plain] = tokenize_turns(GemmaWeightedTokenizer(), weighted_messages(stage, calls=calls), [], **kwargs)
            [weighted] = tokenize_turns(GemmaWeightedTokenizer(), weighted_messages(stage, calls=calls), [], reasoning_weight=.25, **kwargs)
            assert plain['input_ids'] == weighted['input_ids']
            assert plain['labels'] == weighted['labels']
            assert plain['original_message_index'] == weighted['original_message_index']
            assert set(plain['token_weight']) == {1.0}
            assert weighted['labels'][-1] == 49  # handoff remains supervised
            assert 50 not in weighted['input_ids']  # synthetic response opener excluded
            start = weighted['prompt_tokens']
            assert weighted['token_weight'][start:] == [1, 1, 1, .25, .25, .25, 1] + [1]*4*calls
            assert weighted['labels'][:start] == [IGNORE_INDEX]*start


def test_gemma_no_thought_and_no_call_text_remain_full_weight():
    for calls in [0, 1, 2]:
        [example] = tokenize_turns(GemmaWeightedTokenizer(), weighted_messages(reasoning='', calls=calls), [], reasoning_weight=.25, supervise_end_token=True)
        assert set(example['token_weight']) == {1.0}


def test_gemma_tool_turn_without_reasoning_supervises_native_empty_thought():
    # Gemma with thinking ON emits `<|channel>thought\n<channel|>` before a call even when it skips
    # thinking; the template renders nothing for empty reasoning, so the block is supplied.
    for calls in [1, 2]:
        [example] = tokenize_turns(GemmaWeightedTokenizer(), weighted_messages(reasoning='', calls=calls), [], supervise_end_token=True)
        prompt = example['prompt_tokens']
        assert example['input_ids'][prompt:prompt + 5] == [100, 300, 108, 101, 48]
        assert example['labels'][prompt:prompt + 4] == [100, 300, 108, 101]
    [text_turn] = tokenize_turns(GemmaWeightedTokenizer(), weighted_messages(reasoning='', calls=0), [], supervise_end_token=True)
    assert 100 not in text_turn['input_ids'][text_turn['prompt_tokens']:]


def test_gemma_native_thought_template_drops_newline_before_close(monkeypatch):
    from rca_lab.train.checkpoint import gemma_native_thought_template

    class Tok:
        chat_template = "{{- '<|channel>thought\\n' + thinking_text + '\\n<channel|>' -}}"
    patched = gemma_native_thought_template(Tok()).chat_template
    assert "thinking_text + '<channel|>'" in patched and "'\\n<channel|>'" not in patched
    monkeypatch.setenv("NATIVE_THOUGHT_CLOSE", "0")
    assert gemma_native_thought_template(Tok()).chat_template == Tok.chat_template
    monkeypatch.delenv("NATIVE_THOUGHT_CLOSE")

    class Other:
        chat_template = "<|start|>assistant to=self<|message|>{{ reasoning }}<|eom|>"
    assert gemma_native_thought_template(Other()).chat_template == Other.chat_template


def test_gemma_unknown_or_duplicate_markers_fail_closed_only_for_nonunit_weight():
    import pytest
    for bad in [0, 100]:
        tok = GemmaWeightedTokenizer()
        tok.markers = dict(tok.markers, **{'<channel|>': bad})
        # Weight1 takes no new segmentation path, preserving old preprocessing.
        tokenize_turns(tok, weighted_messages(), [], reasoning_weight=1)
        with pytest.raises(ValueError, match='distinct known'):
            tokenize_turns(tok, weighted_messages(), [], reasoning_weight=.25)


def test_gemma_malformed_rendered_spans_fail_closed():
    import pytest
    for replacement in [[100, 300, 108, 700, 48, 800, 49],  # missing thought end
                        [100, 300, 108, 700, 101, 101, 48, 800, 49],
                        [100, 300, 108, 700, 101, 48, 800],  # missing handoff
                        [100, 300, 108, 700, 101, 48, 800, 49, 48, 800, 49]]:
        class Malformed(GemmaWeightedTokenizer):
            def apply_chat_template(self, messages, **kwargs):
                ids = super().apply_chat_template(messages, **kwargs)
                return ids if kwargs.get('add_generation_prompt') else ids[:4] + replacement + [50]
        with pytest.raises(ValueError, match='Gemma'):
            tokenize_turns(Malformed(), weighted_messages(), [], reasoning_weight=.25)


def test_gemma_no_source_reasoning_cannot_hide_rendered_thought():
    import pytest
    class UnexpectedThought(GemmaWeightedTokenizer):
        def apply_chat_template(self, messages, **kwargs):
            messages = [dict(m) for m in messages]
            if not kwargs.get('add_generation_prompt'):
                messages[-1]['reasoning_content'] = 'unexpected'
            return super().apply_chat_template(messages, **kwargs)
    with pytest.raises(ValueError, match='no source reasoning'):
        tokenize_turns(UnexpectedThought(), weighted_messages(reasoning=''), [], reasoning_weight=.25)
