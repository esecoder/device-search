"""Multi-hop: the answer is in a document the first result only points at."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from device_search.agent import Candidate
from device_search.multi_hop import extract_references, follow
from device_search.store import Store


class Doc:
    def __init__(self, path, text):
        self.path = path
        self.mtime = 1.0
        self.size = len(text)
        self.lang = "md"
        self.n_lines = 1
        self.text = text
        self.method = "utf8"
        self.is_dir = False


def main() -> int:
    db = Path(tempfile.mkdtemp()) / "hop.db"
    store = Store(db)

    # The retry document names the limits file. The ANSWER - 30 seconds - exists
    # only in limits. No single-hop search over retry.md can produce it.
    store.add_many([
        Doc("/proj/docs/retry.md",
            "The retry policy is described here. For the numeric values see "
            "config/limits.yaml which is the source of truth."),
        Doc("/proj/config/limits.yaml",
            "retry_timeout_seconds: 30\nmax_attempts: 5"),
        Doc("/proj/README.md", "This project retries things. see config/limits.yaml"),
        Doc("/proj/src/app.py", "from retry import Retry  # unrelated"),
        Doc("/proj/lonely.txt", "nothing points anywhere. no paths at all."),
    ])
    store.conn.commit()

    def ident(path):
        return store.conn.execute(
            "SELECT id FROM documents WHERE path LIKE ?", ("%" + path,)).fetchone()[0]

    print("reference extraction")
    for ref in extract_references(
            "see config/limits.yaml and docs/retry.md, not README.md or package.json"):
        print(f"  {ref}")
    ok_refs = ("config/limits.yaml" in
               extract_references("see config/limits.yaml"))

    print()
    print("hop 2 - the answer is only in the referenced file")
    hop1 = [Candidate(doc_id=ident("docs/retry.md"), path="/proj/docs/retry.md",
                      lang="md", n_lines=1, score=1.0)]
    added, trace = follow(store, hop1)
    for _, path in added:
        print(f"  added: {path}")

    answer_reachable = False
    for doc_id, _path in added:
        row = store.by_id(doc_id)
        if row and "30" in (row[4] or ""):
            answer_reachable = True
    print(f"  hop 1 alone contains the answer : {'30' in (store.by_id(hop1[0].doc_id)[4] or '')}")
    print(f"  after hop 2 the answer is present: {answer_reachable}")

    print()
    print("control - a document that references nothing")
    lonely = [Candidate(doc_id=ident("lonely.txt"), path="/proj/lonely.txt",
                        lang="txt", n_lines=1, score=1.0)]
    added2, trace2 = follow(store, lonely)
    print(f"  added: {len(added2)} (expect 0), hop: {trace2['hop']}")

    print()
    print("control - README is skipped, so hop 2 does not add noise")
    readme = [Candidate(doc_id=ident("README.md"), path="/proj/README.md",
                        lang="md", n_lines=1, score=1.0)]
    added3, trace3 = follow(store, readme)
    print(f"  added: {[p for _, p in added3]}")

    failures = []
    if not ok_refs:
        failures.append("reference extraction missed config/limits.yaml")
    if not added:
        failures.append("hop 2 added nothing")
    if not answer_reachable:
        failures.append("the answer is still unreachable after hop 2")
    if added2:
        failures.append("a document with no references produced a hop")
    if not any("limits" in p for _, p in added3):
        failures.append("README's reference to limits.yaml was not followed")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  the second hop reaches an answer the first cannot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
