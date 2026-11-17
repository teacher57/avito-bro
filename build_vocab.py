"""Word list for the buyer-typo fixer (brain/brain.py fix_typos): every word seen >= 3 times in the old chats
(buyers and shop). A buyer word of 5+ letters that is not in the list and is one edit away from a listed word
is replaced by the most frequent such word. Run: python build_vocab.py  -> data/vocab.json"""
import json, re
from collections import Counter
from pathlib import Path
HERE = Path(__file__).parent
words = Counter()
for c in json.loads((HERE / "data" / "chats_all.json").read_text(encoding="utf-8")):
    for m in c["messages"]:
        words.update(re.findall(r"[а-яё]+", (m.get("text") or "").lower()))
vocab = {w: n for w, n in words.items() if n >= 3}
(HERE / "data" / "vocab.json").write_text(json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
print(f"wrote data/vocab.json: {len(vocab)} words")
