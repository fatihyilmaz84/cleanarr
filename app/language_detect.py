"""Identify the language of a subtitle track from its own text.

Some releases ship subtitle tracks with no language tag *and* no title —
ffprobe returns a literally empty tag set — so there is nothing for
app/normalizer.py to name them from. Their text, though, says plainly what
they are. This reads a sample of it and answers with an ISO 639-2 code.

Deliberately dependency-free and deliberately timid. Two mechanisms:

  - Script. A track written in Greek, Hebrew, Thai, Hangul or Devanagari
    has effectively announced its language already, and no word list is
    needed. Scripts shared by several languages (Cyrillic, Han) are
    narrowed by characters unique to one of them.
  - Function words, for the Latin-script languages, which no script test
    can separate. Dialogue is dense in them, so the fraction of tokens that
    are a language's function words separates languages well.

The output is written into people's media files as a language tag, so a
wrong answer is worse than no answer: a guess must clear both an absolute
floor and a margin over the runner-up, or this returns None and the track
stays untagged. That is why closely related pairs — Danish/Norwegian,
Czech/Slovak — mostly resolve to None here, which is the honest result
rather than a coin flip.
"""

from __future__ import annotations

import re
import unicodedata

# A sample this short is dialogue fragments ("Hi.", "- Lousy.") and carries
# too few function words to separate anything.
MIN_TOKENS = 40
# Fraction of tokens that must be function words of the winning language.
MIN_SCORE = 0.10
# How far ahead of the runner-up the winner must be. Danish and Norwegian
# share most of their common words, so on a Danish sample both score
# similarly and neither clears this — which is the point.
MIN_MARGIN = 1.6

_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
# SRT cue numbers, timestamps, and the markup tracks carry.
_CUE_RE = re.compile(r"^\d+$|^[\d:,.\->\s]+$")
_TAG_RE = re.compile(r"<[^>]+>|\{[^}]*\}")


def _script_of(char: str) -> str | None:
    code = ord(char)
    if 0x0400 <= code <= 0x04FF:
        return "cyrillic"
    if 0x0370 <= code <= 0x03FF:
        return "greek"
    if 0x0590 <= code <= 0x05FF:
        return "hebrew"
    if 0x0600 <= code <= 0x06FF:
        return "arabic"
    if 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF:
        return "hangul"
    if 0x3040 <= code <= 0x30FF:
        return "kana"
    if 0x4E00 <= code <= 0x9FFF:
        return "han"
    if 0x0E00 <= code <= 0x0E7F:
        return "thai"
    if 0x0900 <= code <= 0x097F:
        return "devanagari"
    if 0x0B80 <= code <= 0x0BFF:
        return "tamil"
    if 0x0C00 <= code <= 0x0C7F:
        return "telugu"
    if 0x0C80 <= code <= 0x0CFF:
        return "kannada"
    if 0x0D00 <= code <= 0x0D7F:
        return "malayalam"
    if 0x10A0 <= code <= 0x10FF:
        return "georgian"
    if 0x0530 <= code <= 0x058F:
        return "armenian"
    if 0x0980 <= code <= 0x09FF:
        return "bengali"
    if "LATIN" in unicodedata.name(char, ""):
        return "latin"
    return None


# Scripts used by exactly one language this app knows about.
_SINGLE_LANGUAGE_SCRIPTS = {
    "greek": "gre",
    "hebrew": "heb",
    "hangul": "kor",
    "kana": "jpn",
    "thai": "tha",
    "devanagari": "hin",
    "tamil": "tam",
    "telugu": "tel",
    "kannada": "kan",
    "malayalam": "mal",
    "georgian": "geo",
    "armenian": "arm",
    "bengali": "ben",
}

# Letters that appear in one Cyrillic language and not its neighbours.
_CYRILLIC_MARKERS = {
    "ukr": set("їієґ"),
    "rus": set("ыэё"),
    "bul": set("щъ"),
    "srp": set("ђћџњљ"),
    "mac": set("ѓќѕ"),
}

# Persian/Urdu use letters Arabic does not.
_ARABIC_MARKERS = {"per": set("پچژگ"), "urd": set("ٹڈڑںے")}

# Latin-script function words. Twenty-odd of the commonest per language is
# plenty for dialogue, and keeping the lists short keeps the overlap between
# related languages visible rather than hidden behind rare vocabulary.
_STOPWORDS: dict[str, frozenset[str]] = {
    "eng": frozenset("the a an and is are was were you i he she it we they to of in that for on with have has do don't not what this".split()),
    "dut": frozenset("de het een en is zijn was ik je jij hij zij we wij ze dat die niet naar van in op met voor maar hoe wat er heb".split()),
    "ger": frozenset("der die das und ist sind war ich du er sie wir ihr nicht ein eine zu von mit für auf dass was wie aber noch".split()),
    "fre": frozenset("le la les un une et est sont était je tu il elle nous vous ils pas de du des à en que qui pour avec mais".split()),
    "spa": frozenset("el la los las un una y es son era yo tú él ella nosotros no de del que para con por pero como más está".split()),
    "por": frozenset("o a os as um uma e é são era eu tu ele ela nós não de do da que para com por mas como mais está você".split()),
    "ita": frozenset("il lo la i gli le un una e è sono era io tu lui lei noi non di del che per con ma come più questo".split()),
    "swe": frozenset("och är var det den en ett jag du han hon vi ni de inte att på för med som men har vad här ska".split()),
    "dan": frozenset("og er var det den en et jeg du han hun vi i de ikke at på for med som men har hvad her skal".split()),
    "nor": frozenset("og er var det den en et jeg du han hun vi de ikke at på for med som men har hva her skal jo".split()),
    "fin": frozenset("ja on oli se ei minä sinä hän me te he että kuin mutta jos niin kun vain nyt tämä siitä olen".split()),
    "pol": frozenset("i w z na nie to jest są był jestem ja ty on ona my wy oni że się do od tak ale co jak".split()),
    "cze": frozenset("a v na se je jsou byl nejsem já ty on ona my vy oni že do od tak ale co jak to ne".split()),
    "tur": frozenset("bir ve bu şu o ben sen biz siz onlar için ile de da ne mi mı var yok ama çok daha gibi değil".split()),
    "rum": frozenset("și în de la cu un o este sunt era eu tu el ea noi voi ei nu ce care pentru dar mai".split()),
    "hun": frozenset("a az és van vannak volt én te ő mi ti ők nem hogy de is már csak ez azt mit nagyon".split()),
    "ind": frozenset("yang di dan ke dari itu ini saya kamu dia kita kami mereka tidak ada untuk dengan pada akan sudah bisa".split()),
    "vie": frozenset("của và là các một có không tôi bạn anh chị em chúng họ được cho với trong đã sẽ này đó".split()),
    "hrv": frozenset("i u na je su bio nisam ja ti on ona mi vi oni da se ne ali što kako to za od".split()),
    "cat": frozenset("el la els les un una i és són era jo tu ell ella nosaltres no de del que per amb però com".split()),
}


def _clean_subtitle_text(raw: str) -> str:
    """Strip SRT cue numbers, timestamps and markup, leaving dialogue."""
    lines = []
    for line in raw.splitlines():
        line = _TAG_RE.sub(" ", line).strip()
        if not line or _CUE_RE.match(line):
            continue
        lines.append(line)
    return "\n".join(lines)


def _dominant_script(text: str) -> tuple[str | None, str]:
    """The script most of the letters are in, plus the letters themselves."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return None, ""
    counts: dict[str, int] = {}
    for c in letters:
        script = _script_of(c)
        if script:
            counts[script] = counts.get(script, 0) + 1
    if not counts:
        return None, ""
    # Kana settles Japanese even when Han characters outnumber it.
    if counts.get("kana", 0) >= 5:
        return "kana", "".join(letters)
    top = max(counts, key=counts.get)
    if counts[top] / len(letters) < 0.5:
        return None, "".join(letters)
    return top, "".join(letters).lower()


def _narrow_by_markers(letters: str, markers: dict[str, set[str]], default: str) -> str | None:
    hits = {lang: sum(letters.count(ch) for ch in chars) for lang, chars in markers.items()}
    best = max(hits, key=hits.get)
    if hits[best] >= 3 and hits[best] >= 2 * sorted(hits.values())[-2]:
        return best
    return default


def score_languages(text: str) -> dict[str, float]:
    """Fraction of tokens matching each language's function words. Exposed
    for tests and for explaining a decision.
    """
    tokens = [t.lower() for t in _TOKEN_RE.findall(text)]
    if not tokens:
        return {}
    return {lang: sum(1 for t in tokens if t in words) / len(tokens) for lang, words in _STOPWORDS.items()}


def detect_language(raw_text: str) -> str | None:
    """ISO 639-2 code for `raw_text`, or None when not confident enough.

    None is a normal, expected answer — it means the track keeps whatever it
    already had rather than being tagged with a guess.
    """
    text = _clean_subtitle_text(raw_text)
    if not text.strip():
        return None

    script, letters = _dominant_script(text)
    if script in _SINGLE_LANGUAGE_SCRIPTS:
        return _SINGLE_LANGUAGE_SCRIPTS[script]
    if script == "cyrillic":
        return _narrow_by_markers(letters, _CYRILLIC_MARKERS, default="rus")
    if script == "arabic":
        return _narrow_by_markers(letters, _ARABIC_MARKERS, default="ara")
    if script == "han":
        return "chi"
    if script != "latin":
        return None

    tokens = _TOKEN_RE.findall(text)
    if len(tokens) < MIN_TOKENS:
        return None

    scores = score_languages(text)
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, best_score), (_runner_up, runner_score) = ranked[0], ranked[1]

    if best_score < MIN_SCORE:
        return None
    if runner_score > 0 and best_score < runner_score * MIN_MARGIN:
        return None
    return best
