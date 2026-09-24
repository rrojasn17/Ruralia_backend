from __future__ import annotations
import json
import os
from fastapi import HTTPException


def _client():
    try:
        from openai import OpenAI
    except Exception as exc:
        raise RuntimeError("Cliente OpenAI no disponible") from exc
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY no configurada")
    return OpenAI(api_key=api_key)


def interpret_field_text(*, operation: str, text: str, context: dict) -> dict:
    model = os.getenv("AI_FIELD_MODEL", "gpt-5-mini")
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "intent": {"type": "string", "enum": ["planting", "harvest"]},
            "quantity": {"type": ["number", "null"], "minimum": 0},
            "greenhouse_code": {"type": ["string", "null"]},
            "bed_code": {"type": ["string", "null"]},
            "side": {"type": ["string", "null"], "enum": ["A", "B", None]},
            "requisition_number": {"type": ["string", "null"]},
            "source_lot": {"type": ["string", "null"]},
            "batch_code": {"type": ["string", "null"]},
            "bloom_1_qty": {"type": ["number", "null"], "minimum": 0},
            "bloom_2_qty": {"type": ["number", "null"], "minimum": 0},
            "bloom_3_5_qty": {"type": ["number", "null"], "minimum": 0},
            "discard_qty": {"type": ["number", "null"], "minimum": 0},
            "discard_reason": {"type": ["string", "null"]},
            "notes": {"type": ["string", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "intent", "quantity", "greenhouse_code", "bed_code", "side",
            "requisition_number", "source_lot", "batch_code",
            "bloom_1_qty", "bloom_2_qty", "bloom_3_5_qty",
            "discard_qty", "discard_reason", "notes", "confidence", "warnings"
        ],
    }
    instructions = (
        "Eres un parser de captura de campo para FloresVolcan. El sistema digitaliza el Excel Lirio del cliente. "
        "Respeta su vocabulario: invernadero, cama, lado A/B, lote de bulbo, requisición, siembra y corta. "
        "En una misma cama pueden existir varios lotes/requisiciones y la cosecha ocurre en varios cortes parciales. "
        "Para cosecha reconoce explícitamente las categorías 1 BL, 2 BL, 3-5 BL y descarte con motivo. "
        "No inventes identificadores, lotes, camas ni cantidades. Si un dato no aparece usa null. "
        "La IA solo prellena; el backend validará los datos y el operario debe confirmar."
    )
    try:
        response = _client().responses.create(
            model=model,
            instructions=instructions,
            input=json.dumps({"operation": operation, "text": text, "context": context}, ensure_ascii=False),
            text={"format": {"type": "json_schema", "name": "floresvolcan_field_capture", "strict": True, "schema": schema}},
            max_output_tokens=900,
        )
        output = str(getattr(response, "output_text", "") or "").strip()
        if not output:
            raise ValueError("OpenAI no devolvió contenido")
        data = json.loads(output)
        data["requires_confirmation"] = True
        return data
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo interpretar la captura de campo: {exc}") from exc


def transcribe_field_audio(filename: str, content: bytes, media_type: str | None = None) -> str:
    del media_type
    if not content:
        raise HTTPException(status_code=422, detail="Audio vacío")
    if len(content) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio máximo 12 MB")
    import io
    model = os.getenv("AI_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe")
    try:
        stream = io.BytesIO(content)
        stream.name = filename or "campo.webm"
        response = _client().audio.transcriptions.create(model=model, file=stream)
        text = str(getattr(response, "text", "") or "").strip()
        if not text:
            raise ValueError("No se reconoció voz")
        return text
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo transcribir el audio: {exc}") from exc
