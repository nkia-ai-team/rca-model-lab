"""Transcript helpers: fetch commands by line, decode the remote ssh script argument, extract heredoc bodies."""
import json, re, shlex
MAIN = "/tmp/claude-1006/-home-swlee-projects/b9a7e703-9fce-4947-976a-5dd066c7f1f2/scratchpad/recon/main.jsonl"
_cache = None
def calls():
    global _cache
    if _cache is None:
        _cache = {}
        for l in open(MAIN):
            d = json.loads(l); _cache[d['ln']] = d
    return _cache
def cmd(ln): return calls()[ln]['input']['command']
def result(ln): return calls()[ln]['result'] or ''
def ts(ln): return calls()[ln]['ts']
HOST = "work@proxy1.ainexus.ktcloud.com"
def remote_scripts(c):
    """Every ssh remote command: all shell words after the host up to the first unquoted operator, joined by one
    space (what ssh sends), decoded with POSIX quoting (local $VARS are NOT expanded)."""
    out = []
    for m in re.finditer(r'\bssh\b[^\n]*?' + re.escape(HOST) + r' ', c):
        rest = c[m.end():]
        lex = shlex.shlex(rest, posix=True, punctuation_chars=';&|<>()'); lex.whitespace_split = True; lex.commenters = ''
        words = []
        while True:
            t = lex.get_token()
            if t is None or t == '' or (t and set(t) <= set(';&|<>()')):
                break
            words.append(t)
        out.append(' '.join(words))
    return out
def heredocs(s):
    """[(opening line, body)] for every heredoc in s (quoted or unquoted delimiter)."""
    res = []
    for m in re.finditer(r'<<-?\s*([\'"]?)(\w+)\1[^\n]*\n', s):
        delim = m.group(2); start = m.end()
        e = re.search(r'^' + re.escape(delim) + r'\s*$', s[start:], re.M)
        if e is None: continue
        res.append((s[m.start():m.end()].strip(), s[start:start + e.start()]))
    return res
