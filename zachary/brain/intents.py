"""Fast, local intent parser: rules + light fuzzy matching. No network, no LLM.

Phase 2 intents:
  open           "open the fee receipt", "fee receipt kholo", "opne fees"
  find           "find my DSA notes", "fees yesterday" (default when no verb)
  show_projects  "show me all my projects", "mere projects dikhao"
  recent         "recent files", "what did I open yesterday"
Phase 5 intents:
  topic          "show my topics", "topic machine learning", "my subjects"
  resume         "resume where I left off", "reopen my last session", "jahan chhoda tha"
  timeline       "what was I working on last Tuesday", "what did I do yesterday"
  confirm/cancel "yes" / "no" (answers to "Reopen these 6 files?")
Follow-ups (need a previous result list, see ``context.py``):
  select         "open the second one", "doosra wala", "#3", "the last one"
  reveal         "show its folder", "where is it", "iska folder kholo"
  older / newer  "the older one", "purana wala", "the latest one"
  related        "related to this file", "similar to the second one",
                 "files related to fee receipt", "isse related files"

English, Hinglish and Kannada words are accepted; typos in command words are
corrected (only command words, so a filename like "shop receipts" is left alone).
``confidence`` < 0.7 marks a guess (Phase 6 can ask an LLM instead).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from zachary.brain.timeparse import parse_time
from zachary.search.hybrid import query_terms

_TOKEN = re.compile(r"[#\w'ऀ-ॣ०-෿]+", re.UNICODE)

OPEN_VERBS = frozenset(
    """
    open launch kholo khol kholna kholdo khole kholiye kholna tere teri terey tegi
""".split()
)
FIND_VERBS = frozenset(
    """
    find search look locate where get fetch bring dhundo dhoondo dhundho dhoondh dhund
    khojo hudukku huduku hudki
""".split()
)
SHOW_VERBS = frozenset(
    """
    show list display see view dikhao dikha dikhana batao torisu toris thorisu
""".split()
)
PROJECT_WORDS = frozenset("project projects repo repos repository repositories".split())
RECENT_WORDS = frozenset("recent recently lately".split())
FOLDER_WORDS = frozenset("folder directory location containing explorer finder".split())
REVEAL_VERBS = frozenset("reveal locate".split())
PRONOUNS = frozenset(
    """
    it its it's that this isko usko ise use iska uska iski uski iske uske isse usse idu adu
""".split()
)
OLDER = frozenset("older old earlier previous purana purani purane haleya hale".split())
NEWER = frozenset("newer new latest newest naya nayi naye hosa hosadu".split())
TOPIC_WORDS = frozenset("topic topics subject subjects collection collections".split())
YES_WORDS = frozenset(
    "yes yeah yep yup sure ok okay do it go ahead please haan han ha haa ji houdu hoon sari".split()
)
# "reopen them" / "re-open those 8 files" / "haan sab kholo": an answer to "Reopen them?".
# Only these words (plus open verbs, yes words and numbers) may appear, and it needs a yes
# word or "reopen", or an open verb with "them"/"all"/... ("open it" stays a follow-up).
CONFIRM_ALL = frozenset("them those these all everything sab sabhi saare saari".split())
CONFIRM_YES = (YES_WORDS - {"it", "do", "please"}) | {"reopen"}
CONFIRM_WORDS = CONFIRM_ALL | frozenset(
    "re open file files of the do it karo kar again dobara phir please ok okay just now".split()
)
NO_WORDS = frozenset("no nope nah cancel stop dont don't nahi na beda illa".split())
WORK_WORDS = frozenset("working worked work doing do kaam busy".split())
WHAT_WORDS = frozenset("what kya enu".split())
# Questions answered from file contents (Phase 6): "when is my fee due?", "fee kab hai".
QUESTION_LEAD = frozenset(
    """
    when what whats how who whom whose which why is are was were does do did can should
    will kab kitna kitni kitne kaun kaunsa kaunsi kyun kyon yaavaga yenu eshtu
""".split()
)
QUESTION_ANYWHERE = frozenset("kab kitna kitni kitne kaunsa kaunsi kyun yaavaga eshtu".split())
# "which file has my marks" / "where is my resume" look for a file, not an answer.
FILE_ASK_WORDS = frozenset(
    "file files folder folders document documents doc docs pdf pdfs where kahan kidhar elli".split()
)
# Counting / sizes, answered from the index (never the LLM): "how many pdfs do I have".
STAT_THINGS = frozenset(
    """
    file files document documents doc docs pdf pdfs photo photos picture pictures image
    images pic pics video videos movie movies song songs music audio folder folders
    directory directories project projects topic topics code script scripts space storage
    size gb mb spreadsheet spreadsheets presentation presentations slides
    """.split()
)
_EXT_WORD = re.compile(
    r"\b(pdf|docx?|pptx?|xlsx?|csv|txt|md|zip|py|js|ts|java|cpp|jpe?g|png|mp4|mp3)s?\b"
)
# Cleaning / organizing (Phase 7): always a plan to review, never an action.
CLEAN_WORDS = frozenset(
    """
    clean cleanup clear declutter tidy duplicates duplicate dupes junk safai saaf
    """.split()
)
ORGANIZE_WORDS = frozenset("organize organise arrange sort jamao lagao group".split())
UNDO_WORDS = frozenset("undo revert".split())
REMOVE_VERBS = frozenset("remove delete trash hatao hata nikalo bin".split())
# What a clean-up can be about ("remove old installers", "delete empty folders").
CLEAN_TARGETS = frozenset(
    """
    duplicates duplicate dupes installer installers setup setups empty junk temp temporary
    large big huge
    """.split()
)
PLAN_FILLER = frozenset(
    """
    clean cleanup clear up declutter tidy organize organise arrange sort out jamao lagao karo
    kar do my mine me the a an all of in on please stuff things files file folder folders
    together into one place group everything some free space safai saaf undo revert that last
    this it cleanup changes change find show remove delete trash hatao hata nikalo bin
    """.split()
)
LOCATE_LEAD = frozenset("which where wich kaunsa kaunsi kahan kidhar".split())
# Creating files/folders and dictation (new files only; never changes existing ones).
CREATE_VERBS = frozenset("create make new start banao bana banana write".split())
CREATE_THINGS = frozenset(
    """
    file document doc docx note notes folder directory txt textfile notepad markdown md word
    """.split()
)
PASTE_WORDS = frozenset("paste pasted copied clipboard copy".split())
DICTATE_WORDS = frozenset("dictate dictation dictating likh likho".split())
# "take notes", "note down what I say", "write what I say"
_DICTATE_PHRASES = ("take notes", "note down", "write what i say", "type what i say",
                    "write down what i say", "i'll speak", "i will speak")  # fmt: skip
STOP_DICTATION = re.compile(
    r"\b(stop|end|finish|done|close)\s+(the\s+)?(dictation|dictating)\b|\bdone dictating\b"
    r"|\bdictation (band|khatam|stop)\b",
    re.IGNORECASE,
)
CANCEL_DICTATION = re.compile(r"\b(cancel|discard|abort)\s+(the\s+)?dictation\b", re.I)
NEW_PARAGRAPH = re.compile(r"\b(?:new|next)\s+(?:paragraph|line)\b[.,!]?", re.IGNORECASE)
SCRATCH_THAT = re.compile(r"^\s*(scratch that|delete that|undo that)[.!]?\s*$", re.I)


@dataclass(frozen=True, slots=True)
class CreateSpec:
    what: str  # file | folder
    fmt: str  # docx | txt | md
    name: str  # as said ("Hackathon Ideas"), "" = from the content
    place: str  # "downloads" / "the zachary project" / "" (= sorted into Documents/Zachary)
    paste: bool  # content = what was copied last
    dictate: bool  # content = what you say next, until "stop dictation"
    content: str  # "... saying hello world"


_NAME_RE = re.compile(
    r"\b(?:called|named|titled|name it|naam)\s+[\"“']?(.+?)[\"”']?"
    r"(?=\s+(?:in|inside|into|on|at|with|and|containing|saying|that says|mein)\b|[.,!?]?$)",
    re.IGNORECASE,
)
_CONTENT_RE = re.compile(
    r"\b(?:saying|that says|containing|with the text|with text|which says)\s+(.+)$",
    re.IGNORECASE,
)
_PLACE_RE = re.compile(
    r"\b(?:in|inside|into|on)\s+(?:my\s+|the\s+)?([\w .'-]+?)(?:\s+(?:folder|directory))?"
    r"(?=\s+(?:and|with|called|named|containing|saying)\b|[.,!?]?$)",
    re.IGNORECASE,
)


def parse_create(text: str) -> CreateSpec:
    """Details of "create a text file called todo in downloads and paste what I copied"."""
    low = text.lower()
    toks = tokenize(text)
    what = "folder" if any(t in {"folder", "directory"} for t in toks) and not any(
        t in {"file", "document", "doc", "note", "txt", "docx", "markdown"} for t in toks
    ) else "file"  # fmt: skip
    fmt = "docx"
    if "text file" in low or any(t in {"txt", "textfile", "notepad", "plain"} for t in toks):
        fmt = "txt"
    elif any(t in {"markdown", "md"} for t in toks):
        fmt = "md"
    m = _NAME_RE.search(text)
    name = m.group(1).strip() if m else ""
    c = _CONTENT_RE.search(text)
    content = c.group(1).strip() if c and not name.endswith(c.group(1)) else ""
    place = ""
    for pm in _PLACE_RE.finditer(text):
        cand = pm.group(1).strip()
        low_c = cand.lower()
        if low_c not in {"it", "that", "this", "there"} and not low_c.startswith(
            ("a ", "an ", "new ", "the new ")
        ):
            place = cand  # the last "in X" wins ("... called notes in downloads")
    if name and place and place in name:
        place = ""
    dictate = any(t in DICTATE_WORDS for t in toks) or any(p in low for p in _DICTATE_PHRASES)
    paste = any(t in PASTE_WORDS for t in toks)
    return CreateSpec(what, fmt, name, place, paste, dictate, content)


def needs_clipboard(text: str) -> bool:
    """True when this request uses what was copied (read only then)."""
    toks = tokenize(text)
    return any(t in PASTE_WORDS for t in toks) and (
        any(t in CREATE_VERBS for t in toks) or any(t in CREATE_THINGS for t in toks)
    )


# "explain the last project I was working on", "summarize my dsa notes", "tell me about it"
EXPLAIN_WORDS = frozenset(
    "explain summarize summarise summary describe overview recap samjhao samjha samjhana".split()
)
EXPLAIN_FILLER = frozenset(
    """
    explain summarize summarise summary describe overview recap samjhao samjha samjhana
    tell about give me a an the of my mine i was were am been on what is it this that
    briefly quickly short please kya hai ke ki ka mujhe
    """.split()
)
RELATED_WORDS = frozenset("related similar relevant sambandhit sambandhita sambandhisida".split())
_JAISA = frozenset("jaisa jaise jaisi".split())
RELATED_FILLER = frozenset(
    """
    related similar relevant like more to this these that files file stuff things documents
    docs other others iss is isse isko iske iski se jaisa jaise jaisi sambandhit sambandhita
    sambandhisida idakke with
""".split()
)
ORDINALS: dict[str, int] = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5, "sixth": 6, "6th": 6, "seventh": 7, "7th": 7, "eighth": 8, "8th": 8,
    "ninth": 9, "9th": 9, "last": -1,
    "pehla": 1, "pehli": 1, "pahla": 1, "pahli": 1, "doosra": 2, "dusra": 2, "doosri": 2,
    "dusri": 2, "teesra": 3, "tisra": 3, "teesri": 3, "tisri": 3, "chautha": 4, "chauthi": 4,
    "aakhri": -1, "akhri": -1,
    "modala": 1, "modaladu": 1, "modalane": 1, "eradane": 2, "eradanedu": 2, "moorane": 3,
    "mooraneya": 3, "mooranedu": 3, "koneya": -1, "konedu": -1,
}  # fmt: skip
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9}  # fmt: skip
# Words that carry no content in a command ("open *the* second *one* please").
FILLER = frozenset(
    """
    the a an one ones please pls plz kindly now just me my mine for of on in to with
    can could would will you i want need hey ok okay yo so and
    wala wali wale waala waali ko ka ki ke ye yeh wo woh vo voh hai na zara jara bhai yaar ji
    do de dena madu maadu maadi file files document doc item result option choice version
    number no is was
""".split()
)
PROJECT_FILLER = frozenset(
    """
    all every my mine files file across storage drive drives disk disks everywhere computer
    laptop pc system list the of in on me mere meri saare sare sab sabhi nanna ella entire
    whole ones code coding
""".split()
)
RECENT_FILLER = frozenset(
    """
    what which did do i you open opened opening opens edit edited editing work worked working on
    files file documents docs stuff things was were have had been the my me show list
    recent recently lately last kya maine kholi khola kholi thi tha kaam kar raha rahi
    opens forgot forget remember name named called
    madida maadida madide maadide nodida noDida terede teredidde kelsa
""".split()
)

# Command words eligible for typo correction: English only (Hinglish/Kannada verbs are
# short and collide with ordinary words, e.g. "mere" vs "tere"), never content words.
_FUZZY_VOCAB = sorted(
    {"open", "launch", "find", "search", "locate", "show", "list", "display", "projects",
     "project", "repository", "repositories", "recent", "recently", "folder", "directory",
     "first", "second", "third", "fourth", "fifth", "older", "newer", "latest",
     "related", "similar", "explain", "summarize", "describe", "clean", "organize",
     "duplicates", "undo"}
)  # fmt: skip
_VERB_TARGETS = frozenset({"open", "launch", "find", "search", "locate", "show", "list", "display"})


@dataclass(frozen=True, slots=True)
class Intent:
    kind: str  # open|find|show_projects|recent|select|reveal|older|newer|related|topic|
    #            resume|timeline|confirm|cancel|ask|explain|clean|organize|undo|create|
    #            dictate|stats|notes|settings|none
    query: str = ""  # text to search (time phrases included; search parses them)
    ordinal: int | None = None  # 1-based; -1 = last
    action: str = "open"  # for follow-ups: open | reveal
    project_hint: bool = False  # "open the zachary project" -> prefer project folders
    # "open zachary in vscode": the possible app ("vscode") and what to open with it
    # ("zachary"). The handler checks the app is installed; otherwise it's a search.
    app: str = ""
    target: str = ""
    confidence: float = 1.0
    corrected: dict[str, str] = field(default_factory=dict)

    @property
    def is_followup(self) -> bool:
        if self.kind == "related":
            return not self.query  # "related to this file" refers to the shown results
        return self.kind in {"select", "reveal", "older", "newer"}


def tokenize(text: str) -> list[str]:
    out = []
    for m in _TOKEN.finditer(text):
        t = m.group(0).lower().strip("'")
        if t.endswith("'s"):
            t = t[:-2]  # "one's folder" -> "one"
        if t:
            out.append(t)
    return out


def _known(tok: str) -> bool:
    from zachary.search.hybrid import STOPWORDS

    return (
        tok in FILLER or tok in PROJECT_FILLER or tok in RECENT_FILLER or tok in STOPWORDS
        or tok in OPEN_VERBS or tok in FIND_VERBS or tok in SHOW_VERBS or tok in ORDINALS
        or tok in OLDER or tok in NEWER or tok in PRONOUNS
    )  # fmt: skip


def _one_missing(tok: str, word: str) -> bool:
    """``tok`` is ``word`` with exactly one letter left out ("oldr" / "older")."""
    if len(word) != len(tok) + 1:
        return False
    return any(word[:i] + word[i + 1 :] == tok for i in range(len(word)))


def _fix(tok: str, position: int) -> str:
    """Correct a misspelled command word ("opne", "shwo", "porjects", "secnd").

    Only English command words are targets; known words are never changed; verbs only
    in the first two positions; 4-letter words only by transposition (so "shop" stays).
    """
    if len(tok) < 4 or tok in _FUZZY_VOCAB or _known(tok):
        return tok
    cutoff = 0.75 if len(tok) <= 5 else 0.8
    for word in difflib.get_close_matches(tok, _FUZZY_VOCAB, n=3, cutoff=cutoff):
        if abs(len(word) - len(tok)) > 1:
            continue
        if len(tok) <= 4 and sorted(tok) != sorted(word) and not _one_missing(tok, word):
            continue
        if word in _VERB_TARGETS and position > 1:
            continue
        return word
    return tok


def _ordinal(tokens: list[str]) -> tuple[int | None, set[int]]:
    """Find an ordinal ("second", "2nd", "#2", "number 2", "open 2"); returns (n, token idxs)."""
    for i, t in enumerate(tokens):
        if t in ORDINALS:
            return ORDINALS[t], {i}
        if t.startswith("#") and t[1:].isdigit():
            return int(t[1:]), {i}
        if t.isdigit() and 0 < int(t) < 50:
            prev = tokens[i - 1] if i else ""
            if (
                prev in {"number", "no", "item", "result", "option"}
                or prev in OPEN_VERBS
                or (i == len(tokens) - 1 and len(tokens) <= 3)
            ):
                return int(t), {i}
        if t in NUMBER_WORDS and i and tokens[i - 1] in {"number", "no"}:
            return NUMBER_WORDS[t], {i}
    return None, set()


_QUESTION_TYPOS = {"whn": "when", "wen": "when", "wat": "what", "wht": "what", "hw": "how"}


_SETTINGS_FILLER = frozenset("open show change my the zachary zach zachs app please".split())
_NOTES_FILLER = frozenset(
    "please zach zack hey the my a an zachary zacharys app application window now up".split()
)
_NOTES_OPEN = {("notes",), ("note",), ("open", "notes"), ("open", "note"), ("show", "notes"),
               ("launch", "notes"), ("start", "notes")}  # fmt: skip
_NOTES_NEW = {("new", "note"), ("create", "note"), ("create", "new", "note"), ("make", "note"),
              ("make", "new", "note"), ("start", "new", "note"), ("open", "new", "note"),
              ("write", "note"), ("new", "notes")}  # fmt: skip
_NOTES_DICTATE = {("take", "note"), ("dictate", "note"), ("dictate", "new", "note")}


def notes_action(toks: list[str]) -> str:
    """ "open | new | dictate" for the notes app, "" for anything else. Opening needs a
    clear sign it's the app ("notes app", "my notes", "Zachary notes"): plain "open
    notes" may be a file called notes."""
    core = tuple(t for t in toks if t not in _NOTES_FILLER)
    if core in _NOTES_DICTATE:
        return "dictate"
    if core in _NOTES_NEW:
        return "new"
    if core in _NOTES_OPEN and any(t in {"app", "application", "my", "zachary"} for t in toks):
        return "open"
    return ""


def parse(text: str) -> Intent:
    raw = tokenize(text)
    if raw and raw[0] in _QUESTION_TYPOS:
        raw[0] = _QUESTION_TYPOS[raw[0]]
    raw = raw[_lead_fillers(raw) :]  # "hey zach, ..." / "please ..."
    corrected: dict[str, str] = {}
    toks: list[str] = []
    for i, t in enumerate(raw):
        f = _fix(t, i)
        if f != t:
            corrected[t] = f
        toks.append(f)
    conf = 0.8 if corrected else 1.0
    if not toks:
        return Intent("none", confidence=1.0)
    if toks[0] in {"ask", "question", "puchho", "pucho"} and len(toks) > 1:
        # "ask when is my fee due": always a question (also what the LLM router sends).
        return Intent("ask", query=" ".join(raw[1:]), confidence=conf, corrected=corrected)

    has_open = any(t in OPEN_VERBS for t in toks)
    has_find = any(t in FIND_VERBS for t in toks)
    has_show = any(t in SHOW_VERBS for t in toks)
    has_folder = any(t in FOLDER_WORDS for t in toks)
    has_pronoun = any(t in PRONOUNS for t in toks)
    older = any(t in OLDER for t in toks)
    newer = any(t in NEWER for t in toks)
    ordinal, ord_idx = _ordinal(toks)
    verbs = OPEN_VERBS | FIND_VERBS | SHOW_VERBS | REVEAL_VERBS

    # --- yes: an answer to a proposal ("Reopen them?"); before follow-ups so that
    # "yes open all of them" isn't taken as "open the first result" -------------------
    joined_re = " ".join(toks).replace("re open", "reopen").split()
    if all(
        t in YES_WORDS or t in CONFIRM_YES or t in OPEN_VERBS or t in CONFIRM_WORDS or t.isdigit()
        for t in joined_re
    ) and (
        any(t in CONFIRM_YES for t in joined_re)
        or (any(t in OPEN_VERBS for t in joined_re) and any(t in CONFIRM_ALL for t in joined_re))
    ):
        return Intent("confirm", confidence=conf, corrected=corrected)

    # --- Zachary's settings ("open settings", "preferences") ------------------------------
    if tuple(t for t in toks if t not in _SETTINGS_FILLER) in {("settings",), ("setting",),
                                                               ("preferences",)}:  # fmt: skip
        return Intent("settings", query=text.strip(), confidence=conf, corrected=corrected)

    # --- Zachary's own notes app ("open my notes", "new note", "take a note") -----------
    notes = notes_action(toks)
    if notes:
        return Intent("notes", query=text.strip(), action=notes, confidence=conf,
                      corrected=corrected)  # fmt: skip

    # "new document" / "new text file": a new file, not "the newer one".
    if toks[0] == "new" and len(toks) <= 3 and any(t in CREATE_THINGS for t in toks[1:]):
        return Intent("create", query=text.strip(), confidence=conf, corrected=corrected)

    # --- follow-ups: nothing but command words, ordinals and filler ---------------
    content = [
        t for i, t in enumerate(toks)
        if i not in ord_idx and t not in FILLER and t not in verbs and t not in PRONOUNS
        and t not in FOLDER_WORDS and t not in OLDER and t not in NEWER
        and t not in {"where", "which", "kahan", "kidhar", "elli", "ide", "hai"}
    ]  # fmt: skip
    where_q = any(t in {"where", "kahan", "kidhar", "elli"} for t in toks)
    if not content:
        reveal = has_folder or where_q or any(t in REVEAL_VERBS for t in toks)
        action = "reveal" if reveal else "open"
        if ordinal is not None:
            return Intent("select", ordinal=ordinal, action=action, confidence=conf,
                          corrected=corrected)  # fmt: skip
        if older or newer:
            return Intent("older" if older else "newer", action=action, confidence=conf,
                          corrected=corrected)  # fmt: skip
        if reveal and (has_pronoun or has_folder or where_q):
            return Intent("reveal", action="reveal", confidence=conf, corrected=corrected)
        if has_pronoun and (has_open or has_show):
            return Intent("select", ordinal=None, action="open", confidence=conf,
                          corrected=corrected)  # fmt: skip

    # --- no: an answer to a proposal ("Reopen these 6 files?") ----------------------
    plain = [t for t in toks if t not in {"please", "ok", "okay", "just", "now", "then"}]
    if (
        plain
        and all(t in NO_WORDS or t in FILLER for t in toks)
        and any(t in NO_WORDS for t in toks)
    ):
        return Intent("cancel", confidence=conf, corrected=corrected)

    # --- "which project was I working on about crime scenes", "where is my resume",
    # "which file has my marks": the LLM picks among your things; then it's opened -----
    if toks[0] in LOCATE_LEAD and len(toks) >= 3 and not has_open:
        tr = parse_time(" ".join(toks)).range
        about = any(t in {"about", "regarding", "related", "wala", "wali"} for t in toks)
        if tr is not None and not about:  # "which project was I working on yesterday"
            return Intent("timeline", query=" ".join(toks), confidence=conf,
                          corrected=corrected)  # fmt: skip
        is_project = any(t in PROJECT_WORDS or t in FOLDER_WORDS for t in toks)
        return Intent("ask", query=text.strip(), action="locate", project_hint=is_project,
                      confidence=conf, corrected=corrected)  # fmt: skip

    # --- create a file / folder, dictation ---------------------------------------------
    joined_low = " ".join(toks)
    wants_dictation = any(t in DICTATE_WORDS for t in toks) or any(
        p in joined_low for p in _DICTATE_PHRASES
    )
    makes = any(t in CREATE_VERBS for t in toks) and any(t in CREATE_THINGS for t in toks)
    if wants_dictation and not has_open:
        return Intent("dictate", query=text.strip(), confidence=conf, corrected=corrected)
    if makes and not has_open and not any(t in CLEAN_WORDS for t in toks):
        return Intent("create", query=text.strip(), confidence=conf, corrected=corrected)
    if any(t in PASTE_WORDS for t in toks) and any(t in CREATE_THINGS for t in toks):
        return Intent("create", query=text.strip(), confidence=conf, corrected=corrected)

    # --- clean / organize / undo: destructive -> always a plan (CLAUDE.md) ---------------
    if any(t in UNDO_WORDS for t in toks) and not any(t in ORGANIZE_WORDS for t in toks):
        return Intent("undo", confidence=conf, corrected=corrected)
    free_space = "free" in toks and "space" in toks
    remove_kind = any(t in REMOVE_VERBS for t in toks) and any(t in CLEAN_TARGETS for t in toks)
    if (any(t in CLEAN_WORDS for t in toks) or free_space or remove_kind) and not has_open:
        rest = [t for t in toks if t not in PLAN_FILLER]
        return Intent("clean", query=" ".join(rest), confidence=conf, corrected=corrected)
    if any(t in ORGANIZE_WORDS for t in toks):
        rest = [t for t in toks if t not in PLAN_FILLER]
        return Intent("organize", query=" ".join(rest), confidence=conf, corrected=corrected)

    # --- a question about a project ("what is the zachary project about", "how do I run
    # my hackfest project", "what does this project do"): answered from its files -------
    asks = toks[0] in QUESTION_LEAD or text.strip().endswith("?")
    # Singular: one project ("what is the zachary project about"). "projects" asks for
    # a list ("what are my projects", "which projects did I work on").
    names_project = any(t in {"project", "repo", "repository"} for t in toks)
    counting_projects = "many" in toks and any(
        toks[i + 1] in PROJECT_WORDS for i, t in enumerate(toks[:-1]) if t == "many"
    )  # "how many projects do I have" is a count
    listing = any(t in SHOW_VERBS or t in {"list", "all"} for t in toks)
    if asks and names_project and not counting_projects and not listing and len(toks) >= 3:
        return Intent("ask", query=text.strip(), project_hint=True, confidence=conf,
                      corrected=corrected)  # fmt: skip

    # --- explain / summarize a project, topic or the shown item ---------------------------
    tell_about = "tell" in toks and "about" in toks
    if any(t in EXPLAIN_WORDS for t in toks) or tell_about:
        rest = [t for t in toks if t not in EXPLAIN_FILLER]
        # "explain the second one" refers to a shown result; "the last project" doesn't.
        pick = [t for i, t in enumerate(rest) if toks.index(t) not in ord_idx]
        if ordinal is not None and not [t for t in pick if t not in {"one", "result", "file"}]:
            return Intent("explain", ordinal=ordinal, confidence=conf, corrected=corrected)
        if has_pronoun and not rest:
            return Intent("explain", confidence=conf, corrected=corrected)
        return Intent("explain", query=" ".join(rest), confidence=conf,
                      project_hint=any(t in PROJECT_WORDS for t in toks),
                      corrected=corrected)  # fmt: skip

    # --- resume where I left off -------------------------------------------------------
    joined = " ".join(toks)
    # ("resume" alone is also a file: "open my resume" must stay a search.)
    resume_ctx = {"session", "work", "working", "last", "from", "left", "off"}
    if (
        ("resume" in toks and any(t in resume_ctx for t in toks))
        or "left off" in joined
        or "last session" in joined
        or ("reopen" in toks and not content)
        or "chhoda" in toks
        or "choda" in toks
        or "nilsida" in toks
    ):
        return Intent("resume", confidence=conf, corrected=corrected)

    # --- timeline: what was I working on --------------------------------------------
    file_words = {"file", "files", "document", "documents", "doc", "docs", "open", "opened"}
    if (
        "timeline" in toks
        or "sessions" in toks
        or (
            any(t in WHAT_WORDS for t in toks)
            and any(t in WORK_WORDS for t in toks)
            and not any(t in file_words for t in toks)
        )
    ):
        return Intent("timeline", query=joined, confidence=conf, corrected=corrected)

    # --- topics ---------------------------------------------------------------------------
    if any(t in TOPIC_WORDS for t in toks):
        rest = [t for t in toks if t not in TOPIC_WORDS and t not in FILLER and t not in verbs
                and t not in PROJECT_FILLER and t not in {"all", "about", "on"}]  # fmt: skip
        return Intent("topic", query=" ".join(rest), confidence=conf, corrected=corrected)

    # --- related files ----------------------------------------------------------------
    like_this = "like" in toks and (
        "more" in toks
        or (
            "this" in toks
            and any(t in {"file", "files", "one", "ones", "doc", "docs"} for t in toks)
        )
    )
    rel = (
        any(t in RELATED_WORDS for t in toks)
        or like_this
        or (any(t in _JAISA for t in toks) and has_pronoun)
    )
    if rel:
        lead = 0  # command verbs only at the start ("show me files similar to linked list")
        while lead < len(toks) and (toks[lead] in verbs or toks[lead] in FILLER):
            lead += 1
        rest = [
            t for i, t in enumerate(toks)
            if i >= lead and i not in ord_idx and t not in RELATED_FILLER and t not in FILLER
            and t not in PRONOUNS
        ]  # fmt: skip
        if not rest:
            return Intent("related", ordinal=ordinal, confidence=conf, corrected=corrected)
        return Intent("related", query=" ".join(rest), confidence=conf, corrected=corrected)

    # --- stats: "how many files / pdfs / projects do I have", "how much space ..." -----
    joined_q = " ".join(toks)
    count_q = joined_q.startswith(("how many", "how much", "number of", "count ", "total "))
    count_q = count_q or any(t in {"kitne", "kitni", "kitna", "eshtu", "yestu"} for t in toks)
    if count_q and (any(t in STAT_THINGS for t in toks) or _EXT_WORD.search(joined_q)):
        return Intent("stats", query=joined_q, confidence=conf, corrected=corrected)

    # --- projects -------------------------------------------------------------------
    if any(t in PROJECT_WORDS for t in toks):
        rest = [t for t in toks if t not in PROJECT_WORDS and t not in PROJECT_FILLER
                and t not in verbs and t not in FILLER
                and t not in {"what", "which", "are", "were", "have", "do"}]  # fmt: skip
        if not rest:
            return Intent("show_projects", confidence=conf, corrected=corrected)
        query = " ".join(t for t in raw if corrected.get(t, t) not in verbs and t not in FILLER)
        target, app = ("", "")
        if has_open:  # split before filler removal ("in" is a filler word)
            target, app = split_app(" ".join(t for t in raw if corrected.get(t, t) not in verbs))
            target = " ".join(
                t for t in target.split() if t not in PROJECT_WORDS and t not in FILLER
            )
        return Intent("open" if has_open else "find", query=query, project_hint=True,
                      confidence=conf, corrected=corrected, app=app, target=target)  # fmt: skip

    # --- recent ---------------------------------------------------------------------
    tp = parse_time(" ".join(toks))
    leftover = [
        t for t in query_terms(tp.rest)
        if t not in RECENT_FILLER and t not in verbs and _fix(t, 0) not in verbs
    ]  # fmt: skip
    if (tp.range is not None or any(t in RECENT_WORDS for t in toks)) and not leftover:
        # The handler re-parses the time phrase from the query.
        return Intent("recent", query=" ".join(toks), confidence=conf, corrected=corrected)

    # --- ask: a question answered from file contents -------------------------------
    question = text.strip().endswith("?") and len(toks) >= 3
    lead_q = toks[0] in QUESTION_LEAD and len(toks) >= 3
    if (
        (lead_q or question or any(t in QUESTION_ANYWHERE for t in toks))
        and not any(t in FILE_ASK_WORDS for t in toks)
        and toks[0] not in verbs
        and not has_open
        and len([t for t in toks if t not in FILLER and t not in QUESTION_LEAD]) >= 1
    ):
        return Intent("ask", query=_strip_lead(" ".join(raw)), confidence=conf,
                      corrected=corrected)  # fmt: skip

    # --- open / find ----------------------------------------------------------------
    query = _strip_lead(" ".join(t for t, f in zip(raw, toks, strict=True) if f not in verbs))
    if has_open:
        target, app = split_app(query)
        return Intent("open", query=query, confidence=conf, corrected=corrected, app=app,
                      target=target)  # fmt: skip
    if has_find or has_show:
        return Intent("find", query=query, confidence=conf, corrected=corrected)
    return Intent("find", query=query, confidence=min(conf, 0.6), corrected=corrected)


_APP_SEP = frozenset("in with using via mein me se".split())


def split_app(query: str) -> tuple[str, str]:
    """("zachary in vscode") -> ("zachary", "vscode"); ("", "") if there's no split.
    The app part is 1-3 words after the last "in" / "with" / "using" / "mein"."""
    toks = query.split()
    # Hindi order: "zachary ko vscode mein (kholo)": the app comes before "mein"/"se".
    if len(toks) >= 3 and toks[-1] in {"mein", "me", "se"}:
        body = toks[:-1]
        if "ko" in body:
            k = len(body) - 1 - body[::-1].index("ko")
            head, app = body[:k], body[k + 1 :]
        else:
            head, app = body[:-1], body[-1:]
        if head and 1 <= len(app) <= 3:
            return " ".join(head), " ".join(app)
    for i in range(len(toks) - 1, 0, -1):
        if toks[i] in _APP_SEP and 1 <= len(toks) - i - 1 <= 3:
            head = [t for t in toks[:i] if t not in {"ko", "the", "my", "folder", "file"}]
            if head:
                return " ".join(head), " ".join(toks[i + 1 :])
    return "", ""


_LEAD = frozenset("hey ok okay zach zachary please pls plz can could would you".split())


_WAKE = frozenset({"zach", "zachary"})
_COMMAND_START = (
    OPEN_VERBS
    | FIND_VERBS
    | SHOW_VERBS
    | frozenset(
        "please can could what when how who where which clean organize undo make create explain"
        " summarize tell".split()
    )
)


def _lead_fillers(tokens: list[str]) -> int:
    """Leading "hey zach, please ..." words. "zach(ary)" counts as the wake word only
    after hey/ok or right before a command ("zachary open fee receipt"), not when it's
    the thing to open ("zachary ko vscode mein kholo" = the Zachary project)."""
    n = 0
    for i, t in enumerate(tokens):
        if t in _WAKE:
            prev_greet = i > 0 and tokens[i - 1] in {"hey", "ok", "okay", "hi", "hello"}
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            before_command = nxt in _COMMAND_START
            if not (prev_greet or before_command):
                break
            n += 1
        elif t in _LEAD:
            n += 1
        else:
            break
    return n


def _strip_lead(query: str) -> str:
    toks = query.split()
    while toks and toks[0] in _LEAD and toks[0] not in _WAKE:
        toks.pop(0)
    return " ".join(toks)
