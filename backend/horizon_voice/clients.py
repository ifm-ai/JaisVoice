from __future__ import annotations

import base64
import io
import wave

import httpx
from k2_runtime.prompts import cleaner_messages

from .config import ASR_LANGUAGES, MAX_AUDIO_SECONDS, Settings
from .cleanup_guard import cleanup_problem


class PipelineError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_asr_language(language: str | None) -> str | None:
    if language is None or language == "":
        return None
    if not isinstance(language, str) or language not in ASR_LANGUAGES:
        raise PipelineError("asr_language", "La langue de transcription sélectionnée n’est pas prise en charge.")
    return language


def validate_cleanup_guard(value: bool | str | None) -> bool:
    if value is None or value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise PipelineError("cleanup_guard_setting", "Le réglage du garde-fou est invalide.")


def validate_model_size(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or value not in {"1B", "4B"}):
        raise PipelineError("model_selection", "Choisissez le modèle 1B ou 4B.")
    return value


def validate_wav(data: bytes, max_bytes: int) -> None:
    if len(data) > max_bytes:
        raise PipelineError("audio_too_long", "L’enregistrement dépasse 30 secondes.")
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
                raise PipelineError("audio_format", "Le fichier doit être un WAV PCM 16 bits, mono, 16 kHz.")
            count = audio.getnframes()
            if count > 16_000 * MAX_AUDIO_SECONDS:
                raise PipelineError("audio_too_long", "L’enregistrement dépasse 30 secondes.")
            frames = audio.readframes(count)
            if count < 1600:
                raise PipelineError("audio_empty", "L’enregistrement est trop court. Maintenez le raccourci pour parler.")
            if len(frames) != count * 2:
                raise PipelineError("audio_incomplete", "Le fichier audio est incomplet.")
    except (wave.Error, EOFError, ValueError) as exc:
        raise PipelineError("audio_format", "Le fichier audio n’est pas un WAV valide.") from exc


class InferenceClients:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http

    async def transcribe(self, audio: bytes, *, language: str | None = None) -> str:
        language = validate_asr_language(language)
        validate_wav(audio, self.settings.max_audio_bytes)
        try:
            response = await self.http.post(
                self.settings.asr_url,
                json={"audio": base64.b64encode(audio).decode("ascii"), **({"language": language} if language else {})},
                timeout=180,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PipelineError("asr_unavailable", "La transcription a échoué. Vérifiez l’état du modèle local JAIS-ASR F5.") from exc
        if not isinstance(result, dict):
            raise PipelineError("asr_invalid", "Le modèle ASR local a renvoyé une réponse invalide.")
        if result.get("refused"):
            raise PipelineError("asr_refused", "Le modèle ASR local a refusé cet enregistrement.")
        if result.get("truncated"):
            raise PipelineError("asr_truncated", "La transcription est incomplète. Réessayez avec un extrait plus court.")
        text = result.get("text")
        if not isinstance(text, str) or not text.strip():
            raise PipelineError("asr_empty", "Aucune parole n’a été reconnue.")
        return text.strip()

    async def clean(self, transcript: str, *, model_size: str | None = None, cleanup_guard_enabled: bool = True) -> str:
        model_size = validate_model_size(model_size)
        cleanup_guard_enabled = validate_cleanup_guard(cleanup_guard_enabled)
        try:
            response = await self.http.post(
                self.settings.mlx_url + "/v1/chat/completions",
                json={
                    "model": "k2-cleaner",
                    **({"model_size": model_size} if model_size else {}),
                    "messages": cleaner_messages(transcript),
                    "temperature": 0,
                    "max_tokens": 2048,
                    "reasoning_effort": self.settings.cleaner_reasoning_effort,
                    "stream": False,
                },
                timeout=180,
            )
            response.raise_for_status()
            result = response.json()
            choice = result["choices"][0]
            text = choice["message"].get("content")
            finish = choice.get("finish_reason")
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise PipelineError("cleaner_unavailable", "Le nettoyage a échoué. Vérifiez l’état des modèles locaux.") from exc
        if finish != "stop":
            raise PipelineError("cleaner_incomplete", "Le correcteur n’a pas terminé le nettoyage. La demande n’a pas été exécutée.")
        if not isinstance(text, str) or not text.strip():
            raise PipelineError("cleaner_empty", "Le correcteur n’a renvoyé aucun texte corrigé. La demande n’a pas été exécutée.")
        if cleanup_guard_enabled and cleanup_problem(transcript, text) is not None:
            raise PipelineError("cleaner_uncertain", "Correction incertaine. Reformulez votre demande ; aucune action n’a été lancée.")
        return text.strip()
