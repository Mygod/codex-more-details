#!/usr/bin/env python3
"""Restore the "More details" -> Quick Chat action in the Codex selection menu.

ChatGPT/Codex desktop 26.1007.x dropped the `onMoreDetails` wiring from the
Codex thread selection overlay and deleted the "start Quick Chat from selected
text" helper (see openai/codex#52483). This script ports the 26.1002.52244
implementation back into the current app (26.1029 and later, which ship an
rspack bundle):

  * The Quick Chat state module gets the selected-text Quick Chat starter back.
    It sends "Tell me more about this" with the selection + source message as a
    targeted reply, exactly like 26.1002.
  * Every copy of the Codex thread selection overlay module passes an
    `onMoreDetails` handler into the shared selection menu, which still renders
    the button.
  * The Quick Chat host component registers a Quick Chat shortcut: Cmd-Shift-Space
    (or Option-Space, if the global "Toggle Mini" shortcut is cleared) toggles
    the floating Quick Chat panel while ChatGPT is frontmost. 26.1007 removed the
    standalone Quick Chat window from the main process, so this cannot be global.

The bundle is read from ChatGPT.app's app.asar without modifying it. Each chunk
is an ES module whose `__webpack_modules__` table holds one function per source
module; Node imports a chunk (which only defines those functions) to get exact
module boundaries. Code is located by its structure rather than by minified
names, names in other modules are reached through the module loader and the
modules' own export maps, and every patched chunk must pass `node --check`. If
anything doesn't match, --emit fails and writes nothing.

The patched chunks are served at runtime by inject.mjs (via launch.sh). The app
bundle itself is never modified: Electron reads its asar integrity hash through
the code signature, so an edited bundle aborts at launch.

Usage:
  python3 more_details_patch.py --emit DIR [--app /Applications/ChatGPT.app]
"""
import argparse
import collections
import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile

APP = "/Applications/ChatGPT.app"
MARKER = "__qcMDRun"
I = r"[\w$]+"     # a minified identifier
M = rf"{I}\.{I}"  # a member of another module, as a module sees it: `a.G` for `var a=n("ChYf")`

# Prints {module id: [start, end, params]} for a chunk, in code points, using V8's own parse of its module table.
MODMAP_JS = r"""
import fs from 'node:fs'; import { pathToFileURL } from 'node:url';
const file = process.argv[2], src = fs.readFileSync(file, 'utf8');
const high = (s) => (s.match(/[\uD800-\uDBFF]/g) ?? []).length;
const { __webpack_modules__: table } = await import(pathToFileURL(file));
const out = {}; let from = 0, cp = 0;
for (const [id, fn] of Object.entries(table ?? {})) {
  const text = fn.toString(), at = src.indexOf(text, from);
  if (at < 0) throw new Error(`module ${id} not located`);
  cp += at - from - high(src.slice(from, at));
  const len = text.length - high(text);
  out[id] = [cp, cp + len, text.slice(text.indexOf('(') + 1, text.indexOf(')'))];
  cp += len; from = at + text.length;
}
console.log(JSON.stringify(out));
"""


class PatchError(Exception):
    pass


# ---------------------------------------------------------------- asar I/O

def read_asar(path):
    with open(path, "rb") as f:
        four, hsize, _, slen = struct.unpack("<IIII", f.read(16))
        if four != 4:
            raise PatchError("unexpected asar pickle header")
        raw = f.read(slen)
    return json.loads(raw), raw, 8 + hsize


def walk(node, prefix=""):
    for name, child in node.get("files", {}).items():
        p = f"{prefix}/{name}" if prefix else name
        if "files" in child:
            yield from walk(child, p)
        else:
            yield p, child


def entry(header, path):
    node = header
    for part in path.split("/"):
        node = node["files"][part]
    return node


def read_file(asar, base, node):
    with open(asar, "rb") as f:
        f.seek(base + int(node["offset"]))
        return f.read(node["size"])


def one(pattern, text, what):
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise PatchError(f"{what}: expected 1 match, found {len(matches)}")
    return matches[0]


def node_binary():
    node = shutil.which("node")
    if node is None:
        raise PatchError("node not found")
    return node


# ------------------------------------------------------------------ the bundle

class Module:
    """One module factory, `id(module,exports,require){...}`, in one chunk. A module can appear in several chunks."""

    def __init__(self, chunk, mid, start, end, params):
        self.chunk, self.id, self.start, self.end = chunk, mid, start, end
        self.exports, self.require = (params + ["", ""])[1:3]

    @property
    def key(self):
        return self.chunk, self.id


class Bundle:
    """The webview's JavaScript chunks in app.asar, read on demand and patched module by module in memory."""

    def __init__(self, asar, workdir):
        header, _, self.base = read_asar(asar)
        self.asar, self.workdir = asar, workdir
        self.nodes = {path.rsplit("/", 1)[1]: node for path, node in walk(header)
                      if path.startswith("webview/assets/") and path.endswith(".js") and "unpacked" not in node}
        self.raw, self.src, self.maps, self.hits, self.patched = {}, {}, {}, {}, {}
        with open(os.path.join(workdir, "modmap.mjs"), "w") as f:
            f.write(MODMAP_JS)

    def scan(self, hints):
        """Read every chunk once, noting which chunks contain each hint and keeping only those."""
        hints = [h for h in hints if h not in self.hits]
        found = {h: [] for h in hints}
        with open(self.asar, "rb") as f:
            for name, node in self.nodes.items():
                if name in self.raw:
                    data = self.raw[name]
                else:
                    f.seek(self.base + int(node["offset"]))
                    data = f.read(node["size"])
                for h in hints:
                    if h.encode() in data:
                        found[h].append(name)
                        self.raw[name] = data
        self.hits.update(found)

    def source(self, chunk):
        if chunk not in self.src:
            if chunk not in self.raw:
                self.raw[chunk] = read_file(self.asar, self.base, self.nodes[chunk])
            self.src[chunk] = self.raw[chunk].decode("utf-8")
        return self.src[chunk]

    def modules(self, chunk):
        if chunk not in self.maps:
            path = os.path.join(self.workdir, chunk.replace(".js", ".mjs"))
            with open(path, "wb") as f:
                f.write(self.raw[chunk] if chunk in self.raw else self.source(chunk).encode())
            r = subprocess.run([node_binary(), os.path.join(self.workdir, "modmap.mjs"), path],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise PatchError(f"couldn't read the module table of {chunk}:\n{r.stderr[-1000:]}")
            self.maps[chunk] = [Module(chunk, mid, s, e, p.split(",")) for mid, (s, e, p) in json.loads(r.stdout).items()]
        return self.maps[chunk]

    def module_at(self, chunk, pos):
        for mod in self.modules(chunk):
            if mod.start <= pos < mod.end:
                return mod
        raise PatchError(f"no module at {chunk}:{pos}")

    def module(self, chunk, mid):
        for mod in self.modules(chunk):
            if mod.id == mid:
                return mod
        raise PatchError(f"{chunk} has no module {mid}")

    def text(self, mod):
        return self.patched.get(mod.key) or self.source(mod.chunk)[mod.start:mod.end]

    def set_text(self, mod, text):
        self.patched[mod.key] = text

    def find(self, pattern, what, hint, count=1):
        """[(module, match)] for pattern in chunks containing hint; count=None allows any number above zero."""
        self.scan([hint])
        found = []
        for chunk in self.hits[hint]:
            src = self.source(chunk)
            for m in re.finditer(pattern, src):
                found.append((self.module_at(chunk, m.start()), m))
        if (len(found) != count) if count else not found:
            raise PatchError(f"{what}: expected {count or 'at least 1'} match(es), found {len(found)}")
        for mod, _ in found:
            if MARKER in self.source(mod.chunk)[mod.start:mod.end]:
                raise PatchError(f"{mod.chunk} is already patched")
        return found

    def output(self):
        """{chunk: patched source} for every chunk with a patched module."""
        out = {}
        for chunk in sorted({c for c, _ in self.patched}):
            src = self.source(chunk)
            for mod in sorted((m for m in self.modules(chunk) if m.key in self.patched), key=lambda m: -m.start):
                src = src[:mod.start] + self.patched[mod.key] + src[mod.end:]
            out[chunk] = src
        return out


# ------------------------------------------------------------------ linking

def module_var(b, mod, var):
    """The module id a module-level `var x=require("id")` in mod refers to."""
    m = re.search(rf'(?<![\w$.]){re.escape(var)}={re.escape(mod.require)}\("([^"]+)"\)', b.text(mod))
    if m is None:
        raise PatchError(f"{mod.id}: no module variable {var}")
    return m.group(1)


def skip(text, j):
    """Index just past the string, template literal or bracketed span starting at text[j]."""
    q = text[j]
    if q in "\"'":
        j += 1
        while text[j] != q:
            j += 2 if text[j] == "\\" else 1
        return j + 1
    if q == "`":
        j += 1
        while text[j] != "`":
            if text[j] == "\\":
                j += 2
            elif text.startswith("${", j):
                j = skip(text, j + 1)
            else:
                j += 1
        return j + 1
    depth = 0
    while True:
        c = text[j]
        if c in "\"'`":
            j = skip(text, j)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1


def object_entries(text, i):
    """Top-level `key:value` entries of the object literal at text[i] == "{", and the index just past it."""
    entries, start, j = [], i + 1, i + 1
    while text[j] != "}":
        c = text[j]
        if c in "\"'`([{":
            j = skip(text, j)
            continue
        if c == ",":
            entries.append(text[start:j])
            start = j + 1
        j += 1
    entries.append(text[start:j])
    return [e for e in entries if e], j + 1


def export_map(b, mod):
    """(index of the first export object or None, {local or `x.y`: exported name}) from mod's `require.d(exports, ...)`
    calls. Their getter and value objects can hold inline functions (26.1103), so only plain names are read."""
    text, names, first = b.text(mod), {}, None
    for m in re.finditer(rf"{re.escape(mod.require)}\.d\({re.escape(mod.exports)},(?=\{{)", text):
        first = m.end() if first is None else first
        getters, j = object_entries(text, m.end())
        values, _ = object_entries(text, j + 1) if text.startswith(",{", j) else ([], j)
        for part, arrow in [(g, ":()=>") for g in getters] + [(v, ":") for v in values]:
            key, sep, value = part.partition(arrow)
            if sep and re.fullmatch(r"[\w$]+(?:\.[\w$]+)?", value):
                names.setdefault(value, key.strip('"'))
    return first, names


def export(b, mod, local, name=None):
    """The name mod exports local as, adding a getter export if it has none."""
    first, names = export_map(b, mod)
    if local in names:
        return names[local]
    name = name or f"__qc_{local}"
    text = b.text(mod)
    if first is not None:
        at = first + 1
        b.set_text(mod, text[:at] + f"{name}:()=>{local}," + text[at:])
    else:
        at = text.index("{") + 1
        b.set_text(mod, text[:at] + f"{mod.require}.d({mod.exports},{{{name}:()=>{local}}});" + text[at:])
    return name


def ref(b, mod, token):
    """(module id, export name) for a name as written in mod: `x.y` (a member of another module) or a local."""
    if "." in token:
        var, member = token.split(".", 1)
        return module_var(b, mod, var), member
    return mod.id, export(b, mod, token)


def prelude(b, mod, core_id):
    """Give mod a module-level loader alias and a guarded accessor for the patched Quick Chat module."""
    text = b.text(mod)
    if "__qcReq" not in text:
        at = text.index("{") + 1
        b.set_text(mod, text[:at] + f"var __qcReq={mod.require};function __qcCore(){{try{{return __qcReq(\"{core_id}\")}}"
                   "catch{return null}}" + text[at:])


def req(mid, name):
    return f'__qcReq("{mid}").{name}'


# ------------------------------------------------------------------ patterns

QC = r'\{name:"chatgpt\.quick-chat"\}'

# The toggle's header. The call that shows the panel, show(e,r.hasConversation), is looked up in its body.
TOGGLE_RE = rf"function ({I})\(e,t=\{{\}}\)\{{if\(!e\.get\(({M}),{QC}\)\.isCapable\)return;let n=e\.get\(({I})\),r=e\.get\(({I})\);"

# The new-session function opens the session with an inline function; its default supplies the id generator.
# Its other options vary (26.1103 added previewSession), so only the first is matched.
OPENER_RE = (rf"\((?P<fn>function\(e,\{{conversationId:t=\(0,(?P<newid>{M})\)\(\),contextSourceThread:[^}}]*\}}=\{{\}}\)"
             rf"\{{[^{{}}]*(?:\{{[^{{}}]*\}}[^{{}}]*)*\}})\)\(e,")

SUBMIT_RE = (rf"let {I}=e\.get\((?P<model>{M}),t\);e\.set\((?P<submitting>{M}),t,!0\);try\{{let (?P<hints>{I})={I}\?\?"
             rf"e\.get\({M},t\),{I}=null=={I}\|\|0===(?P<prompt>{I})\.trim\(\)\.length\?void 0:\(0,(?P<build>{M})\)\(\{{"
             rf"messageMetadata:{I},oneTurnDeveloperInstructions:{I},prompt:(?P=prompt),systemHints:(?P=hints)\}}\),"
             rf"{I}=await \(0,(?P<send>{M})\)\(e,\{{conversationId:t,")

# Pending prompt (null) and pending markdown (false) atoms, around a derived "last user message" atom, then lists.
PENDING_RE = (rf"(?P<prompt>{I})=\(0,(?P<f>{M})\)\((?P<s>{M}),e=>null\),(?:{I}=\(0,{M}\)\((?P=s),\(e,\{{get:t\}}\)=>[^;]*?\),)?"
              rf"(?P<markdown>{I})=\(0,(?P=f)\)\((?P=s),e=>!1\),{I}=\(0,(?P=f)\)\((?P=s),e=>\[\]\),"
              rf"{I}=\(0,(?P=f)\)\((?P=s),e=>\[\]\),{I}=\(0,(?P=f)\)\((?P=s),e=>\[\]\)")

SERVER_ID_RE = (rf"(?<![\w$.])({I})=\(0,{M}\)\({M},\(e,\{{get:t\}}\)=>null==e\?null:\(0,{M}\)\(e\)\?t\({M},e\):"
                rf"\(0,{M}\)\(e\)\)")

STREAMING_RE = (rf"(?<![\w$.])({I})=\(0,{M}\)\({M},\(e,\{{get:t\}}\)=>\{{let n=\(0,{M}\)\(t,e\);if\(null==n\|\|!{I}\("
                rf"t\({I},e\)\)\)return!1;let r=t\({M},n\);return null==r\|\|t\({M},n\)!==r\}}\)")

# Calls of the message text extractor. Since 26.1103 it is an anonymous function in its module's export map, so
# it is found through its callers.
PLAIN_TEXT_RE = rf"\(0,({M})\)\({I},\{{includeCollapsedContent:!0\}}\)"

# The Quick Chat host component, up to and including its useLayoutEffect call. 26.1103 added a paneBody prop.
HOST_RE = (rf"\{{(?:{I}:{I},)*surfaceOwner:(?P<owner>{I})(?:,{I}:{I})*\}}=(?P<props>{I}),(?P<store>{I})=\(0,{M}\)\({M}\);"
           rf"(?P<t>{I})\[0\]===Symbol\.for\(\"react\.memo_cache_sentinel\"\)\?\((?P<k>{I})={QC},(?P=t)\[0\]=(?P=k)\):"
           rf"(?P=k)=(?P=t)\[0\];let (?P<a>{I})=\(0,{M}\)\((?P<bo>{M}),(?P=k)\)\.isCapable,(?P<o>{I})=\(0,{M}\)\("
           rf"(?P<disabled>{M})\),(?P<s>{I})=(?P=a)&&!(?P=o);return\((?P=t)\[1\]!==(?P=s)\|\|(?P=t)\[2\]!==(?P=store)\?"
           rf"\((?P<c>{I})=\(\)=>\{{(?P=s)\|\|\(0,{M}\)\((?P=store)\)\}},(?P<l>{I})=\[(?P=s),(?P=store)\],"
           rf"(?P=t)\[1\]=(?P=s),(?P=t)\[2\]=(?P=store),(?P=t)\[3\]=(?P=c),(?P=t)\[4\]=(?P=l)\):\((?P=c)=(?P=t)\[3\],"
           rf"(?P=l)=(?P=t)\[4\]\),\(0,(?P<react>{I})\.useLayoutEffect\)\((?P=c),(?P=l)\),")

# The shared selection menu calls useIntl() right after defaulting its terminology prop.
USE_INTL_RE = rf"{I}=void 0===({I})\?\"task\":\1,{I}=\(0,({M})\)\(\),\[{I},{I}\]=\(0,{I}\.useState\)"

# The Codex thread selection overlay; rspack hoists the component's temporaries into the leading `let`.
OUTER_RE = (rf"function {I}\((?P<props>{I})\)\{{let (?:{I},)*(?P<t>{I})=\(0,(?P<cache>{I})\.c\)\((?P<size>\d+)\),"
            rf"\{{actions:{I},canOpenSideChat:{I},scopeToThread:{I},targetSelector:(?P<sel>{I})\}}=(?P=props),"
            rf"(?P<store>{I})=\(0,{M}\)\({M}\),")


# ------------------------------------------------------- Quick Chat state

def patch_core(b):
    [(core, m)] = b.find(TOGGLE_RE, "quick chat toggle", 'chatgpt.quick-chat"}).isCapable)return;let n=')
    src = b.text(core)
    toggle, bo, session = m.group(1), m.group(2), m.group(3)
    body = src[m.end() - core.start:src.index("}function ", m.end() - core.start)]
    show = one(rf"({I})\(e,r\.hasConversation\)", body, "quick chat toggle's show call").group(1)
    visible = one(rf"function {re.escape(show)}\(e,t\)\{{let n=!e\.get\(({I})\);e\.set\(\1,!0\),e\.set\({I},n&&!t\?"
                  rf"\"welcome\":\"recent\"\),e\.set\(({I}),!0\)\}}", src, "quick chat visibility atom").group(2)
    opener = one(OPENER_RE, src, "quick chat session opener")
    mark = one(rf"function ({I})\(e,t,n=\{{\}}\)\{{if\(!e\.get\({re.escape(bo)},{QC}\)\.isCapable\)return;let r=e\.get\("
               rf"{re.escape(session)}\);r\?\.conversationId!==t\|\|r\.hasConversation\|\|", src,
               "quick chat conversation-started").group(1)
    close = one(rf"function ({I})\(e,t=\{{\}}\)\{{\(0,{M}\)\(e,0\);let n=e\.get\({re.escape(visible)}\),r=e\.get\("
                rf"{re.escape(session)}\)", src, "quick chat close").group(1)
    # context(e, sourceThread) builds the Codex thread context with background subagents included, as More details
    # always asked for. 26.1103 inlined the underlying builder into it, so find the wrapper from its call.
    call = one(r"\(e,\{isBackgroundSubagentsEnabled:!0,sourceThread:t\}\)\}", src, "Codex context call")
    heads = list(re.finditer(rf"function ({I})\(e,t\)\{{return ", src[:call.start()]))
    if not heads:
        raise PatchError("Codex context builder not found")
    context = heads[-1].group(1)
    thread_host = one(rf"\{{threadId:({I}),hostId:{I}\.get\(({M}),\1\)\}}", src, "thread host atom").group(2)

    table = {"TOGGLE": toggle, "BO": bo, "SESSION": session, "SHOW": show, "VISIBLE": visible, "CLOSE": close,
             "OPENSESSION": opener["fn"], "NEWID": opener["newid"], "MARKSTARTED": mark, "CODEXCONTEXT": context,
             "THREADHOST": thread_host}

    [(mod, m)] = b.find(SUBMIT_RE, "ChatGPT submit helper", "oneTurnDeveloperInstructions:")
    for key, group in (("MODELSETTINGS", "model"), ("SUBMITTING", "submitting"), ("BUILDMESSAGES", "build"),
                       ("SEND", "send")):
        table[key] = req(*ref(b, mod, m[group]))
    [(mod, m)] = b.find(PENDING_RE, "pending prompt atoms", 'findLast(e=>"user"===e.author.role')
    table["PENDINGPROMPT"], table["PENDINGMARKDOWN"] = req(*ref(b, mod, m["prompt"])), req(*ref(b, mod, m["markdown"]))
    [(mod, m)] = b.find(SERVER_ID_RE, "server conversation id atom", "(e,{get:t})=>null==e?null:")
    table["SERVERID"] = req(*ref(b, mod, m.group(1)))
    [(mod, m)] = b.find(STREAMING_RE, "streaming atom", "return null==r||t(")
    table["STREAMING"] = req(*ref(b, mod, m.group(1)))
    extractors = {ref(b, mod, m.group(1)) for mod, m in b.find(PLAIN_TEXT_RE, "plain text extractor",
                                                               "includeCollapsedContent:!0", count=None)}
    if len(extractors) != 1:
        raise PatchError(f"plain text extractor: expected 1 function, found {sorted(extractors)}")
    table["PLAINTEXT"] = req(*extractors.pop())

    # The toast atom, as most modules in this chunk use it.
    toasts = collections.Counter(ref(b, b.module_at(core.chunk, t.start()), t.group(1))
                                 for t in re.finditer(rf"\.get\(({M})\)\.danger\(", b.source(core.chunk)))
    if not toasts:
        raise PatchError("toast atom not found")
    table["TOAST"] = req(*toasts.most_common(1)[0][0])

    # The host's capability gate also checks whether Quick Chat is disabled.
    [(host, h)] = b.find(HOST_RE, "quick chat host", "surfaceOwner")
    if ref(b, host, h["bo"]) != ref(b, core, bo):
        raise PatchError("quick chat host: capability atom doesn't match the toggle's")
    table["DISABLED"] = req(*ref(b, host, h["disabled"]))

    js = r"""var __qcReq=REQUIRE,__qcT,__qcPend=[],__qcCapKey={name:`chatgpt.quick-chat`};/* codex-more-details: port of 26.1002.52244 selected-text Quick Chat */
function __qcN(){return __qcT??=TABLE}
function __qcCan(e){try{return e.get(BO,__qcCapKey).isCapable&&!e.get(DISABLED)}catch{return!1}}
function __qcTrunc(e,t){let L=4e4,R=`\n\n… Codex conversation truncated …\n\n`,Z=`\n\n… selected text shown above …\n\n`;if(e.length<=L)return e;let n=e.indexOf(t);if(n===-1){let r=L-R.length,i=Math.floor(r/2),a=r-i;return`${e.slice(0,i)}${R}${e.slice(-a)}`}let r=n+t.length;return`${e.slice(Math.max(0,n-19983),n)}${Z}${e.slice(r,r+19984)}`}
function __qcBusy(e,t){return e(SUBMITTING,t)||e(SERVERID,t)==null&&e(STREAMING,t)}
async function __qcPrep(e,t,n){let r=t.selectedText.trim();if(r.length===0)return null;let i=__qcTrunc(t.selectedMessageText.trim()||r,r),o=[...await CODEXCONTEXT(e,{threadId:t.sourceConversationId,hostId:e.get(THREADHOST,t.sourceConversationId)}),`The user is referring to this in particular:\n${r}\n\n---\n\nThe selection appears in the following source message. Prioritize this source message over the broader conversation context:\n\n${i}`],a=e.get(MODELSETTINGS,n);return{model:a.slug,thinkingEffort:a.thinkingEffort,userCompletionMessages:BUILDMESSAGES({extraDeveloperInstructions:o,messageMetadata:{is_visually_hidden_from_conversation:!0,targeted_reply:r,targeted_reply_label:t.targetedReplyLabel},prompt:t.prompt})}}
async function __qcStart(e,t){if(!e.get(BO,{name:`chatgpt.quick-chat`}).isCapable)return;let n=e.get(SESSION),i=n?.selectedTextSourceConversationId===t.sourceConversationId?n.conversationId:__qcPend.find(e=>e.sourceConversationId===t.sourceConversationId)?.conversationId??NEWID(),a={conversationId:i,sourceConversationId:t.sourceConversationId};__qcPend=[...__qcPend,a];let o;try{o=await __qcPrep(e,t,i)}finally{__qcPend=__qcPend.filter(e=>e!==a)}if(o==null)return;for(;__qcBusy(e.get,i);)await e.when(({get:e})=>!__qcBusy(e,i));if(!e.get(BO,{name:`chatgpt.quick-chat`}).isCapable)return;let s=e.get(SESSION);e.set(SUBMITTING,i,!0),e.set(PENDINGMARKDOWN,i,!1),e.set(PENDINGPROMPT,i,t.prompt),s?.conversationId===i?e.set(SESSION,{...s,selectedTextSourceConversationId:t.sourceConversationId}):(OPENSESSION(e,{conversationId:i}),e.set(SESSION,{...e.get(SESSION),selectedTextSourceConversationId:t.sourceConversationId})),SHOW(e,!0),MARKSTARTED(e,i);try{await SEND(e,{conversationId:i,model:o.model,prompt:t.prompt,thinkingEffort:o.thinkingEffort,userCompletionMessages:o.userCompletionMessages})}finally{e.set(PENDINGPROMPT,i,null),e.set(SUBMITTING,i,!1)}}
var __qcKeyScopes=[];
function __qcBindKeys(e,n,s){if(n!=null||!s)return;__qcKeyScopes.push(e);window.__qcKeysBound||(window.__qcKeysBound=!0,window.addEventListener(`keydown`,__qcOnKey,!0));return()=>{let t=__qcKeyScopes.lastIndexOf(e);t>=0&&__qcKeyScopes.splice(t,1)}}
function __qcOnKey(t){if(t.repeat||t.code!==`Space`||t.ctrlKey)return;let m=t.metaKey&&t.shiftKey&&!t.altKey,o=t.altKey&&!t.metaKey&&!t.shiftKey;if(!m&&!o)return;let e=__qcKeyScopes[__qcKeyScopes.length-1];if(e==null)return;t.preventDefault(),t.stopPropagation();try{e.get(VISIBLE)&&e.get(SESSION)!=null?CLOSE(e):TOGGLE(e,{})}catch(n){console.error(`[more-details] Quick Chat shortcut failed`,n)}}
function __qcMDRun(e,{selectedText:s,target:l,sourceConversationId:c,intl:I}){if(c==null)return;let m=I.formatMessage({id:`selectedTextOverlay.quickChatPrompt`,defaultMessage:`Tell me more about this`,description:`Prompt sent in Quick Chat when asking about selected text`}),b=I.formatMessage({id:`chatGptConversation.targetedReply.selectedText`,defaultMessage:`Selected text`,description:`Label shown above a Quick Chat message that replies to text selected in a Codex conversation`}),x=null;try{x=l==null?null:PLAINTEXT(l)}catch{}__qcStart(e,{isBackgroundSubagentsEnabled:!0,prompt:m,selectedMessageText:x?.plainText??l?.innerText??``,selectedText:s,sourceConversationId:c,targetedReplyLabel:b}).catch(n=>{console.error(`[Composer] selected text Quick Chat failed`,n);try{e.get(TOAST).danger(I.formatMessage({id:`selectedTextOverlay.quickChatError`,defaultMessage:`Failed to start chat`,description:`Toast shown when asking about selected text in chat fails`}),{errorAnalytics:{toastId:`selectedTextOverlay.quickChatError`}})}catch{}})}
"""
    # Everything the port calls goes through one table built at module scope, so the port's own locals can't
    # shadow a module name (single letters like `a` are both).
    js = re.sub(r"\b(" + "|".join(sorted(table, key=len, reverse=True)) + r")\b", lambda m: f"(0,__qcN().{m.group(1)})", js)
    js = js.replace("TABLE", "{" + ",".join(f"{k}:{v}" for k, v in table.items()) + "}").replace("REQUIRE", core.require)
    text = b.text(core)
    at = text.index("{") + 1
    b.set_text(core, text[:at] + js + text[at:])
    for name in ("__qcMDRun", "__qcCan", "__qcBindKeys"):
        export(b, core, name, name)

    # Register the shortcut while the host is mounted.
    prelude(b, host, core.id)
    text = b.text(host)
    h = one(HOST_RE, text, "quick chat host")
    args = f"{h['store']},{h['owner']},{h['s']}"
    b.set_text(host, text[:h.end()] + f"(0,{h['react']}.useEffect)(()=>__qcCore()?.__qcBindKeys({args}),[{args}]),"
               + text[h.end():])
    return core, table


# ---------------------------------------------------- selection overlays

def patch_overlays(b, core):
    intl = {ref(b, mod, m.group(2)) for mod, m in b.find(USE_INTL_RE, "useIntl in selection menu",
                                                       "conversationTerminology:", count=None)}
    if len(intl) != 1:
        raise PatchError(f"useIntl in selection menu: expected 1 hook, found {sorted(intl)}")
    use_intl = req(*intl.pop())
    for mod, _ in b.find(OUTER_RE, "Codex selection overlay", "canOpenSideChat:", count=None):
        prelude(b, mod, core.id)
        patch_overlay(b, mod, use_intl)


def patch_overlay(b, mod, use_intl):
    # Codex thread overlay: compute an onMoreDetails handler and pass it down.
    src = b.text(mod)
    q = one(OUTER_RE, src, "Codex selection overlay")
    t, cache, size, sel, store = q["t"], q["cache"], int(q["size"]), q["sel"], q["store"]
    T, S, A = re.escape(t), re.escape(store), re.escape(sel)
    q_end = src.index("}function ", q.end())
    body = src[q.start():q_end]
    reacts = set(re.findall(r"\(0,([\w$]+)\.useContext\)", body))
    if len(reacts) != 1:
        raise PatchError(f"React namespace: expected 1 name, found {sorted(reacts)}")
    react = reacts.pop()
    thread_id = one(rf"{T}\[2\]!=={S}\?\((?P<d>{I})=\(0,(?P<id>{M})\)\({S}\),{T}\[2\]={S},{T}\[3\]=(?P=d)\):(?P=d)={T}\[3\]",
                    body, "thread id")["id"]
    jm = one(rf"{T}\[13\]!==(?P<h>{I})\|\|{T}\[14\]!==(?P<g>{I})\|\|{T}\[15\]!==(?P<u>{I})\|\|{T}\[16\]!=={A}\?"
             rf"\((?P<v>{I})=\(0,(?P<jsx>{I})\.jsx\)\((?P<inner>{I}),\{{onAddResponseTextAnnotation:(?P=h),"
             rf"onOpenSideChat:(?P=g),targetContainerRefs:(?P=u),targetSelector:{A}\}}\),{T}\[13\]=(?P=h),"
             rf"{T}\[14\]=(?P=g),{T}\[15\]=(?P=u),{T}\[16\]={A},{T}\[17\]=(?P=v)\)", body, "overlay render")
    h, g, u, v, inner = jm["h"], jm["g"], jm["u"], jm["v"], jm["inner"]
    body = body[:jm.start()] + (
        f"{t}[13]!=={h}||{t}[14]!=={g}||{t}[15]!=={u}||{t}[16]!=={sel}||{t}[{size}]!==__md?({v}=(0,{jm['jsx']}.jsx)("
        f"{inner},{{onAddResponseTextAnnotation:{h},onMoreDetails:__md,onOpenSideChat:{g},targetContainerRefs:{u},"
        f"targetSelector:{sel}}}),{t}[13]={h},{t}[14]={g},{t}[15]={u},{t}[16]={sel},{t}[{size}]=__md,{t}[17]={v})"
    ) + body[jm.end():]
    cut = q.end() - q.start()
    body = body[:cut] + (
        f"__qi={use_intl}(),__qc=__qcCore()?.__qcCan({store})??!1,__md=(0,{react}.useMemo)(()=>__qc?(__s,__l)=>"
        f"__qcCore()?.__qcMDRun({store},{{selectedText:__s,target:__l,sourceConversationId:(0,{thread_id})({store}),"
        f"intl:__qi}}):void 0,[{store},__qi,__qc]),"
    ) + body[cut:]
    body = body.replace(f"(0,{cache}.c)({size})", f"(0,{cache}.c)({size + 1})", 1)
    src = src[:q.start()] + body + src[q_end:]

    # Inner overlay: accept onMoreDetails and hand it to the shared selection menu.
    e = one(rf"function {re.escape(inner)}\((?P<props>{I})\)\{{let (?:{I},)*(?P<t>{I})=\(0,(?P<cache>{I})\.c\)\((?P<size>\d+)\),"
            rf"\{{onAddResponseTextAnnotation:(?P<n>{I}),onOpenSideChat:(?P<r>{I}),targetContainerRefs:{I},targetSelector:{I}\}}"
            rf"=(?P=props),(?P<o>{I})=\(0,{M}\)\(\);return null==(?P=n)&&null==(?P=r)\?null:\((?P=t)\[0\]!==(?P=n)\|\|"
            rf"(?P=t)\[1\]!==(?P=r)\|\|(?P=t)\[2\]!==(?P=o)\?\((?P<s>{I})=", src, "inner overlay")
    t, n, r, o, s, size = e["t"], e["n"], e["r"], e["o"], e["s"], int(e["size"])
    e_end = src.find("}function ", e.end())
    e_end = len(src) - 1 if e_end < 0 else e_end
    body = src[e.start():e_end]
    target = one(rf"\{{portalTarget:{I},rect:{I},selectedText:{I},selectionRange:{I},target:({I})\}}={I}", body,
                 "selection target").group(1)
    head = e.group(0)
    head = head.replace(f"(0,{e['cache']}.c)({size}),{{onAddResponseTextAnnotation:{n},",
                        f"(0,{e['cache']}.c)({size + 1}),{{onAddResponseTextAnnotation:{n},onMoreDetails:__md,", 1)
    head = head.replace(f"return null=={n}&&null=={r}?null:({t}[0]!=={n}||{t}[1]!=={r}||{t}[2]!=={o}?",
                        f"return null=={n}&&null=={r}&&null==__md?null:({t}[0]!=={n}||{t}[1]!=={r}||{t}[2]!=={o}||"
                        f"{t}[{size}]!==__md?", 1)
    if "__md" not in head or head.count("__md") != 3:
        raise PatchError("inner overlay: unexpected header")
    body = head + body[len(e.group(0)):]
    T, N, R, O, S = (re.escape(x) for x in (t, n, r, o, s))
    mp = one(rf"\}},onOpenSideChat:{R}\}}\)\}}\)\}},{T}\[0\]={N},{T}\[1\]={R},{T}\[2\]={O},{T}\[3\]={S}\):{S}={T}\[3\]",
             body, "inner overlay menu props")
    body = body[:mp.start()] + (
        f"}},onMoreDetails:null==__md?void 0:__e=>__md(__e,{target}),onOpenSideChat:{r}}})}})}},"
        f"{t}[0]={n},{t}[1]={r},{t}[2]={o},{t}[{size}]=__md,{t}[3]={s}):{s}={t}[3]") + body[mp.end():]
    b.set_text(mod, src[:e.start()] + body + src[e_end:])


# ------------------------------------------------------------------ main

def node_check(name, text):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, name.replace(".js", ".mjs"))
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        r = subprocess.run([node_binary(), "--check", p], capture_output=True, text=True)
        if r.returncode != 0:
            raise PatchError(f"syntax check failed for {name}:\n{r.stderr[-2000:]}")
    print(f"  syntax OK: {name}")


def app_version(app):
    if os.name == "nt":
        asar = os.path.join(app, "resources", "app.asar")
        header, _, base = read_asar(asar)
        info = json.loads(read_file(asar, base, entry(header, "package.json")))
        return info.get("version"), "Windows"
    with open(os.path.join(app, "Contents/Info.plist"), "rb") as f:
        info = plistlib.load(f)
    return info.get("CFBundleShortVersionString"), info.get("CFBundleVersion")


def patched_bundles(app):
    asar = os.path.join(app, "resources/app.asar" if os.name == "nt" else "Contents/Resources/app.asar")
    with tempfile.TemporaryDirectory() as work:
        b = Bundle(asar, work)
        core, table = patch_core(b)
        patch_overlays(b, core)
        if os.name == "nt":
            # Keep Alt+Space available for the Windows system menu.
            b.set_text(core, b.text(core).replace(
                "t.code!==`Space`||t.ctrlKey)return;let m=t.metaKey&&t.shiftKey&&!t.altKey,o=t.altKey&&!t.metaKey&&!t.shiftKey",
                "t.code!==`Space`||t.metaKey)return;let m=t.ctrlKey&&t.shiftKey&&!t.altKey,o=!1", 1))
        print("  resolved:", ", ".join(f"{k}={v if len(v) < 40 else v[:37] + '...'}" for k, v in sorted(table.items())))
        print("  patched modules:", ", ".join(f"{c}:{m}" for c, m in sorted(b.patched)))
        out = b.output()
    for name, text in out.items():
        node_check(name, text)
    return out


def emit(app, out_dir):
    bundles = patched_bundles(app)
    os.makedirs(out_dir, exist_ok=True)
    for name, text in bundles.items():
        tmp = os.path.join(out_dir, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, os.path.join(out_dir, name))
        print(f"  wrote {name}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", default=APP)
    ap.add_argument("--emit", metavar="DIR", help="write the patched bundles to DIR")
    args = ap.parse_args()
    short, build_no = app_version(args.app)
    print(f"ChatGPT {short} ({build_no}) at {args.app}")
    try:
        if args.emit:
            emit(args.app, args.emit)
        else:
            ap.print_help()
    except PatchError as e:
        sys.exit(f"error: {e}\nNothing was written.")


if __name__ == "__main__":
    main()
