"""The Jais-ASR port on a tiny random model: encoder frame count and the audio merge."""
import numpy as np
import pytest
import mlx.core as mx

from jais_runtime.asr_model import JaisASR, output_length


def reference_length(mel_frames):
    """`_audio_output_lengths` of the repository's processor, which sizes the audio placeholders."""
    leave = mel_frames % 100
    after = (leave - 1) // 2 + 1
    return ((after - 1) // 2 + 1 - 1) // 2 + 1 + (mel_frames // 100) * 13


@pytest.mark.parametrize('frames', [201, 250, 299, 300, 1001, 3001])
def test_encoder_length_matches_the_processor(frames):
    assert output_length(frames) == reference_length(frames)


def tiny_model():
    text = dict(model_type='k2_horizon', hidden_size=32, intermediate_size=64, num_attention_heads=2,
                num_key_value_heads=1, head_dim=16, vocab_size=64, num_hidden_layers=1, layernorm_num_groups=2)
    audio = dict(model_type='qwen3_omni_moe_audio_encoder', d_model=16, encoder_layers=2, encoder_attention_heads=2,
                 encoder_ffn_dim=32, num_mel_bins=16, output_dim=24, downsample_hidden_size=4, max_source_positions=64)
    return JaisASR({'model_type': 'jais_asr', 'audio_token_id': 7, 'text_config': text, 'audio_config': audio})


def test_audio_frames_replace_the_placeholders_in_order():
    model = tiny_model()
    frames = 250
    count = output_length(frames)
    ids = [1, 2] + [7] * count + [3, 4]
    features = mx.array(np.random.default_rng(3).normal(size=(16, frames)).astype(np.float32))
    merged = model.embed(ids, features)[0]
    audio = model.multi_modal_projector(model.audio_tower(features))
    text = model.language_model.model.embed_tokens(mx.array(ids))
    assert merged.shape == (len(ids), 32)
    np.testing.assert_allclose(np.array(merged[2:2 + count]), np.array(audio), rtol=1e-5, atol=1e-5)
    np.testing.assert_array_equal(np.array(merged[[0, 1, -2, -1]]), np.array(text[[0, 1, -2, -1]]))
    with pytest.raises(ValueError, match='placeholders'):
        model.embed(ids[:-3], features)
