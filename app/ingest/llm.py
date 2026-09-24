"""LLM services: description generation + vision, with graceful degradation.

Design (see ARCHITECTURE.md):
- If OPENAI_API_KEY is set, descriptions come from an OpenAI-compatible chat
  model (gpt-4o-mini class) and every call is logged to the llm_usage table
  (provider, model, input/output tokens) — cost accounting.
- If no key is set, a deterministic template-based generator fills in so the
  prototype is fully runnable with zero API keys.
- Vision (photo vs spreadsheet conflict detection): OpenAI vision model if a
  key is set, else local RapidOCR if installed, else skipped with a note.
"""
from __future__ import annotations

import json
import re

import httpx

from .. import db, settings
from ..audit import utcnow


class LLMService:
    """Description generation with usage accounting."""

    purpose = "description_generation"

    def available(self) -> bool:
        return bool(settings.OPENAI_API_KEY)

    def generate_description(self, item: dict, tenant) -> tuple[str, str]:
        """Returns (description, generation_method)."""
        if not self.available():
            return self._template_description(item), "template-fallback"
        try:
            return self._openai_description(item, tenant), "openai"
        except Exception:  # noqa: BLE001 - degrade, never block the pipeline
            return self._template_description(item), "template-fallback"

    def _openai_description(self, item: dict, tenant) -> str:
        prompt = (
            f"Restaurant: {tenant.restaurant_name} ({tenant.cuisine}).\n"
            f"Dish: {item.get('item_name')}\n"
            f"Category: {item.get('category')}\n"
            f"Dietary: {item.get('dietary_type')}\n"
            f"Tags: {', '.join(item.get('tags') or [])}"
        )
        resp = httpx.post(
            f"{settings.OPENAI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            json={
                "model": settings.OPENAI_CHAT_MODEL,
                "temperature": 0.4,
                "max_tokens": 90,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You write concise, appetizing restaurant menu descriptions. "
                            "One or two sentences, under 200 characters, no quotes, no emoji, "
                            "plain text only."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {})
        db.execute(
            "INSERT INTO llm_usage (provider, model, purpose, input_tokens, output_tokens,"
            " item_ref, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                "openai",
                settings.OPENAI_CHAT_MODEL,
                self.purpose,
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                item.get("item_id"),
                utcnow(),
            ),
        )
        text = data["choices"][0]["message"]["content"].strip().strip('"')
        return text[:200]

    def _template_description(self, item: dict) -> str:
        name = item.get("item_name") or "This dish"
        category = (item.get("category") or "our kitchen").lower()
        dietary = (item.get("dietary_type") or "").lower()
        tags = item.get("tags") or []

        bits = [f"{name} from our {category} selection"]
        if dietary in ("veg", "vegan"):
            bits.append("made with fresh plant-based ingredients")
        elif dietary == "egg":
            bits.append("prepared with egg")
        elif dietary == "non-veg":
            bits.append("prepared with our house spices")
        if tags:
            bits.append(f"{tags[0].replace('-', ' ')} style")
        return ", ".join(bits).strip().rstrip(",") + "."


class VisionService:
    """Extract readable text (item name, price) from item photos.

    Provider chain: OpenAI vision (if key) -> RapidOCR (if installed) -> skip.
    """

    purpose = "vision_extraction"

    def available(self) -> str | None:
        """Returns the provider name in use, or None if the pass is skipped."""
        if settings.OPENAI_API_KEY:
            return "openai"
        try:
            import rapidocr_onnxruntime  # noqa: F401

            return "rapidocr"
        except ImportError:
            return None

    def extract(self, image_bytes: bytes, mime: str = "image/jpeg") -> dict | None:
        """Returns {'item_name': str|None, 'price': float|None, 'raw_lines': [...]}"""
        provider = self.available()
        if provider == "openai":
            try:
                return self._openai_extract(image_bytes, mime)
            except Exception:  # noqa: BLE001
                provider = "rapidocr"
        if provider == "rapidocr":
            try:
                return self._rapidocr_extract(image_bytes)
            except Exception:  # noqa: BLE001
                return None
        return None

    def _openai_extract(self, image_bytes: bytes, mime: str) -> dict:
        import base64

        b64 = base64.b64encode(image_bytes).decode()
        resp = httpx.post(
            f"{settings.OPENAI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            json={
                "model": settings.OPENAI_VISION_MODEL,
                "max_tokens": 150,
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You read menu photos. Return JSON: "
                            '{"item_name": string|null, "price": number|null, '
                            '"raw_lines": [strings]} — only text visible in the image.'
                        ),
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Extract the item name and price."},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{b64}"},
                            },
                        ],
                    },
                ],
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        usage = resp.json().get("usage", {})
        db.execute(
            "INSERT INTO llm_usage (provider, model, purpose, input_tokens, output_tokens,"
            " item_ref, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                "openai",
                settings.OPENAI_VISION_MODEL,
                self.purpose,
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                None,
                utcnow(),
            ),
        )
        parsed = json.loads(content)
        price = parsed.get("price")
        return {
            "item_name": parsed.get("item_name"),
            "price": float(price) if isinstance(price, (int, float)) else None,
            "raw_lines": parsed.get("raw_lines") or [],
        }

    def _rapidocr_extract(self, image_bytes: bytes) -> dict:
        ocr = _get_rapidocr()
        result, _ = ocr(image_bytes)
        lines: list[str] = []
        if result:
            for box, text, score in result:
                if float(score) >= 0.6:
                    lines.append(str(text))
        price = None
        name = None
        for line in lines:
            m = re.search(r"(\d{1,3}(?:[.,]\d{2}))", line.replace("$", ""))
            if m and price is None:
                price = float(m.group(1).replace(",", "."))
            elif not re.search(r"\d", line):
                if name is None or len(line) > len(name):
                    name = line
        return {"item_name": name, "price": price, "raw_lines": lines}


_OCR = None


def _get_rapidocr():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR = RapidOCR()
    return _OCR


llm_service = LLMService()
vision_service = VisionService()
