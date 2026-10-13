"""Render the reviewed tool catalog into the README and architecture documents."""
from pathlib import Path
import html
import json

ROOT = Path(__file__).resolve().parents[1]
START = "<!-- TOOL-CATALOG:START -->"
END = "<!-- TOOL-CATALOG:END -->"


def insert(path: Path, content: str, anchor: str) -> None:
    text = path.read_text()
    block = START + "\n" + content + "\n" + END + "\n"
    if START in text:
        before, tail = text.split(START, 1)
        _, after = tail.split(END, 1)
        text = before + block + after.lstrip("\n")
    else:
        assert anchor in text, (path, anchor)
        text = text.replace(anchor, block + anchor, 1)
    path.write_text(text)


def main() -> None:
    tools = json.loads((ROOT / "docs/harness-tool-catalog.json").read_text())
    assert len(tools) == 25 and len({t["name"] for t in tools}) == 25
    esc = html.escape
    rows, cards, md = [], [], []
    for t in tools:
        name = t["name"]
        example = json.dumps(t["example"], ensure_ascii=False, indent=2)
        rows.append(f'| `{name}` | {t["purpose"]} | {t["availability"]} |')
        cards.append(
            f'<article class="tool-card" id="tool-{esc(name)}">'
            f'<span class="tag">{esc(t["category"])}</span> '
            f'<small>{esc(t["availability"])}</small>'
            f'<h3><code>{esc(name)}</code></h3><p>{esc(t["purpose"])}</p>'
            f'<dl><dt>주요 입력</dt><dd>{esc(t["inputs"])}</dd>'
            f'<dt>반환 정보</dt><dd>{esc(t["returns"])}</dd></dl>'
            f'<details><summary>호출 인자 예시 · 구현 위치</summary>'
            f'<pre><code>{esc(example)}</code></pre>'
            f'<a href="../{esc(t["source"])}">{esc(t["source"])}</a>'
            '</details></article>'
        )
        md.append(f'### `{name}`\n\n{t["purpose"]}\n\n'
                  f'- 제공 조건: {t["availability"]}\n- 주요 입력: {t["inputs"]}\n'
                  f'- 반환 정보: {t["returns"]}\n'
                  f'- 구현: [{t["source"]}](../{t["source"]})\n\n'
                  f'```json\n{example}\n```\n')
    intro = ('Go 관측 도구 **23종**, Python 결론 제출 **1종**, '
             'structured 정책 전용 원문 조회 **1종**입니다. '
             'legacy에서는 `read_observation`을 제공하지 않습니다.')
    table = '\n'.join(['| 도구 | 언제 사용하는가 | 제공 조건 |', '|---|---|---|', *rows])
    insert(ROOT / 'README.md', '## 전체 도구 목록\n\n' + intro + '\n\n' + table +
           '\n\n입력·반환·호출 예시는 [HTML 도구 사전](docs/rca-harness-architecture.html#tools)에서 확인할 수 있습니다.\n', '## 시작하기')
    insert(ROOT / 'docs/mcp-harness-architecture.md', '## 도구별 사용법\n\n' + intro +
           '\n\n예시의 UUID·시간·근거 참조는 실제 조회에서 얻은 값으로 바꿉니다.\n\n' + '\n'.join(md), '## 서버에서 서비스 찾기')
    section = '''<section id="tools"><h2>전체 도구 사전</h2>
<p>Go 관측 도구 23종 + Python 결론 제출 1종. 원문 조회 1종은 structured 정책에서 추가됩니다.</p>
<p>각 카드에 주요 입력과 반환 정보를 표시했습니다. 호출 예시의 UUID·시간·근거 참조는 실제 조회 값으로 바꿉니다.</p>
<label for="tool-search">도구 이름·용도로 찾기</label>
<input id="tool-search" type="search" placeholder="예: 서버, 로그, DB, trace" aria-controls="tool-cards">
<p id="tool-count" role="status">25개 도구</p><div id="tool-cards" class="tool-grid">''' + ''.join(cards) + '''</div>
<style>.tool-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.tool-card{border:1px solid var(--line);border-radius:12px;padding:20px;min-width:0}.tool-card h3{margin-top:12px}.tool-card h3 code{font-size:16px}.tool-card dl{font-size:14px}.tool-card dt{font-weight:700;margin-top:12px}.tool-card dd{margin:4px 0;color:var(--muted);overflow-wrap:anywhere}.tool-card pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f8fc;padding:12px}.tool-card a{overflow-wrap:anywhere}.tool-card[hidden]{display:none}#tool-search{display:block;width:100%;margin-top:8px;padding:12px;border:1px solid #9bafc9;border-radius:8px;font:inherit}#tool-search:focus{outline:2px solid var(--blue)}@media(max-width:700px){.tool-grid{grid-template-columns:1fr}}</style>
<script>document.getElementById('tool-search').addEventListener('input',function(){let n=0;const q=this.value.trim().toLowerCase();document.querySelectorAll('.tool-card').forEach(c=>{c.hidden=!c.textContent.toLowerCase().includes(q);if(!c.hidden)n++});document.getElementById('tool-count').textContent=n+'개 도구';});</script></section>'''
    insert(ROOT / 'docs/rca-harness-architecture.html', section, '<section><h2>“이 서버에 어떤 서비스가 있나?”')


if __name__ == '__main__':
    main()
