"""Extract and chunk helpers (spec 33)."""

from shared.knowledge import (
    chunk_text,
    collapse_whitespace,
    embedding_url,
    format_vector,
    guess_source_type,
    strip_html,
)
from shared.knowledge import extract_text
from shared.models import KnowledgeSourceType


class TestGuessAndExtract:
    def test_guess_pdf_from_name(self) -> None:
        assert guess_source_type("rates.pdf") is KnowledgeSourceType.PDF

    def test_extract_txt(self) -> None:
        assert "harbour" in extract_text(b"Wi-Fi password is harbour-1842.", KnowledgeSourceType.TXT)

    def test_extract_csv(self) -> None:
        text = extract_text(b"room,rate\n101,120\n", KnowledgeSourceType.CSV)
        assert "room: 101" in text
        assert "rate: 120" in text

    def test_strip_html_drops_script(self) -> None:
        assert "secret" not in strip_html("<p>Hello</p><script>secret</script>")
        assert "Hello" in strip_html("<p>Hello</p><script>secret</script>")


class TestChunk:
    def test_short_text_is_one_chunk(self) -> None:
        assert chunk_text("Checkout is at 11.") == ["Checkout is at 11."]

    def test_long_text_splits(self) -> None:
        body = " ".join(["Sentence number %d is here." % i for i in range(80)])
        chunks = chunk_text(body, size=120, overlap=20)
        assert len(chunks) > 1
        assert all(chunk for chunk in chunks)

    def test_empty_is_empty(self) -> None:
        assert chunk_text("   ") == []


class TestEmbedHelpers:
    def test_embedding_url_appends_v1(self) -> None:
        assert embedding_url("https://api.openai.com") == "https://api.openai.com/v1/embeddings"

    def test_format_vector(self) -> None:
        literal = format_vector([0.1, -0.2])
        assert literal.startswith("[")
        assert "0.10000000" in literal

    def test_collapse(self) -> None:
        assert collapse_whitespace("a \n\n b") == "a b"
