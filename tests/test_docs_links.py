"""Every relative link, image and #anchor in the README and docs/ points at
something that exists, so moving or renaming a section can't leave a dead link.
Links to websites aren't checked; that would need the network.
"""

from pathlib import Path
from unittest import TestCase, main
from urllib.parse import unquote
import re
import tempfile


ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r'!?\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')


def strip_fences(text):
    """Blanks fenced code blocks, keeping the line numbers."""
    return re.sub(r"^```.*?^```", lambda block: "\n" * block.group(0).count("\n"), text,
                  flags=re.M | re.S)


def anchors(path):
    """The anchors GitHub gives the headings of a Markdown file."""
    seen, found = {}, set()
    for line in strip_fences(path.read_text(encoding="utf-8")).splitlines():
        heading = re.match(r"#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not heading:
            continue
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading.group(1))
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def problems(root):
    """Dead links in root's README.md and docs/, as 'file:line: link (why)'."""
    found = []
    for path in [root / "README.md", *sorted((root / "docs").rglob("*.md"))]:
        # Code can show link syntax that isn't a link.
        text = re.sub(r"`[^`\n]*`", "", strip_fences(path.read_text(encoding="utf-8")))
        for match in LINK.finditer(text):
            target = match.group(1)
            if re.match(r"[a-z][a-z0-9+.-]*:", target, re.I):  # https:, mailto: and the like
                continue
            where = f"{path.relative_to(root)}:{text.count(chr(10), 0, match.start()) + 1}: {target}"
            file_part, _, anchor = target.partition("#")
            dest = (path.parent / unquote(file_part)).resolve() if file_part else path
            if not dest.exists():
                found.append(f"{where} (no such file)")
            elif anchor and dest.suffix == ".md" and anchor not in anchors(dest):
                found.append(f"{where} (no such heading)")
    return found


class DocsLinksTests(TestCase):
    def test_every_link_in_the_readme_and_docs_resolves(self):
        self.assertEqual(problems(ROOT), [])

    def test_dead_files_and_headings_are_reported(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "docs").mkdir()
        (root / "README.md").write_text(
            "# Title\n\n"
            "[fine](docs/a.md#second-part) [gone](docs/missing.md)\n"
            "[bad heading](docs/a.md#nowhere) [same page](#title) ![image](docs/a.md)\n"
            "```\n[in a code block](nowhere.md)\n```\n"
            "`[in inline code](nowhere.md)` [a website](https://example.com/x)\n")
        (root / "docs/a.md").write_text("# A\n\n## Second part\n\n[up](../README.md#title)\n")
        self.assertEqual(problems(root), [
            "README.md:3: docs/missing.md (no such file)",
            "README.md:4: docs/a.md#nowhere (no such heading)",
        ])

    def test_anchors_are_the_ones_github_makes(self):
        page = Path(self.enterContext(tempfile.TemporaryDirectory())) / "page.md"
        page.write_text("# CI/CD Workflow\n## The `latest` tag\n## Notes\n## Notes\n"
                        "```\n# not a heading\n```\n")
        self.assertEqual(anchors(page), {"cicd-workflow", "the-latest-tag", "notes", "notes-1"})


if __name__ == "__main__":
    main()
