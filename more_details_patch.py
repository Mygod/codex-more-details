#!/usr/bin/env python3
"""Restore the "More details" -> Quick Chat action in the Codex selection menu.

ChatGPT/Codex desktop 26.1007.x dropped the `onMoreDetails` wiring from the
Codex thread selection overlay and deleted the "start Quick Chat from selected
text" helper (see openai/codex#52483). This script ports the 26.1002.52244
implementation back:

  * The chunk with the Quick Chat internals (app-initial-*.js) gets the
    selected-text Quick Chat starter back. It sends "Tell me more about this"
    with the selection + source message as a targeted reply, exactly like
    26.1002, and adds a capability hook.
  * The chunk with the Codex thread selection overlay (app-primary-*.js up to
    26.1016, a lazily loaded chunk since 26.1021) passes an `onMoreDetails`
    handler into the shared selection menu, which still renders the button.
  * The Quick Chat host component (app-initial up to 26.1016, app-primary since
    26.1021) registers a Quick Chat shortcut: Cmd-Shift-Space (or Option-Space,
    if the global "Toggle Mini" shortcut is cleared) toggles the floating Quick
    Chat panel while ChatGPT is frontmost. 26.1007 removed the standalone Quick
    Chat window from the main process, so this cannot be global.

The chunks are read from ChatGPT.app's app.asar without modifying it. Code is
located by its structure rather than by minified names, names defined in other
chunks are reached through those chunks' own import and export lists, and every
patched chunk must pass `node --check`. If anything doesn't match (for example
after an update changes the surrounding code), --emit fails and writes nothing.

The patched files are served at runtime by inject.mjs (via launch.sh). The app
bundle itself is never modified: 26.1007's Electron reads its asar integrity
hash through the code signature, so an edited bundle aborts at launch.

Usage:
  python3 more_details_patch.py --emit DIR [--app /Applications/ChatGPT.app]
"""
import argparse
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
I = r"[\w$]+"  # a minified identifier


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


# Rolldown lazy module init: function X(){return(X=helper((()=>{...  The helper's
# minified name changes between releases (`e` in 26.1007, `t` in 26.1013).
INIT_RE = re.compile(r"function ([\w$]+)\(\)\{return\(\1=[\w$]+\(\(\(\)=>\{")


def init_containing(text, assignment_pos):
    """Name of the rolldown lazy-init function whose body contains assignment_pos."""
    best = None
    for m in INIT_RE.finditer(text):
        if m.start() > assignment_pos:
            break
        best = m
    if best is None:
        raise PatchError("could not find module init")
    return best.group(1)


def init_after(text, pos):
    m = INIT_RE.search(text, pos)
    if m is None:
        raise PatchError("could not find module init")
    return m.group(1)


# ----------------------------------------------------------- webview chunks

class Bundle:
    """The webview's JavaScript chunks in app.asar, read on demand and patched in memory."""

    def __init__(self, asar):
        header, _, self.base = read_asar(asar)
        self.asar = asar
        self.nodes = {path.rsplit("/", 1)[1]: node for path, node in walk(header)
                      if path.startswith("webview/assets/") and path.endswith(".js") and "unpacked" not in node}
        self.raw, self.text, self.changed = {}, {}, []

    def __getitem__(self, name):
        if name not in self.text:
            self.text[name] = self.bytes(name).decode("utf-8")
        return self.text[name]

    def __setitem__(self, name, text):
        self.text[name] = text
        if name not in self.changed:
            self.changed.append(name)

    def bytes(self, name):
        if name not in self.raw:
            self.raw[name] = read_file(self.asar, self.base, self.nodes[name])
        return self.raw[name]

    def find(self, pattern, what, hint):
        """The one match of pattern in any chunk, as (chunk, match). Only chunks containing hint are searched."""
        found = [(name, m) for name in self.nodes if hint.encode() in self.bytes(name)
                 for m in re.finditer(pattern, self[name])]
        if len(found) != 1:
            raise PatchError(f"{what}: expected 1 match, found {len(found)}")
        if MARKER.encode() in self.bytes(found[0][0]):
            raise PatchError(f"{found[0][0]} is already patched")
        return found[0]


def exports(text):
    """The chunk's export statement and its {exported name: local name} map."""
    m = re.search(r"export\{([^}]*)\};?\s*(//# sourceMappingURL=[^\n]*)?\s*$", text)
    if m is None:
        raise PatchError("export list not found")
    names = {}
    for part in m.group(1).split(","):
        bits = part.strip().split(" as ")
        names[bits[-1]] = bits[0]
    return m, names


def imports(text):
    """{local alias: (chunk, exported name)} for the chunk's static imports of other chunks."""
    names = {}
    for m in re.finditer(r'import\{([^}]*)\}from"\./([^"]+)";', text):
        for part in m.group(1).split(","):
            bits = part.strip().split(" as ")
            names[bits[-1]] = (m.group(2), bits[0])
    return names


def canonical(bundle, chunk, name):
    """(chunk, local name) where a name used in chunk is actually defined, following its imports."""
    for _ in range(8):
        imported = imports(bundle[chunk]).get(name)
        if imported is None:
            return chunk, name
        chunk, exported = imported
        name = exports(bundle[chunk])[1].get(exported)
        if name is None:
            raise PatchError(f"{chunk} doesn't export {exported}")
    raise PatchError(f"import chain for {name} is too long")


def bind(bundle, target, chunk, local):
    """A name code in target can use for local (defined in chunk), exporting and importing it if needed.

    Only imports target already has are extended, so no chunk starts loading earlier than it did."""
    if target == chunk:
        return local
    m, names = exports(bundle[chunk])
    exported = next((e for e, l in names.items() if l == local), None)
    if exported is None:
        exported = local if local.startswith("__qc") else f"__qc_{local}"
        src = bundle[chunk]
        bundle[chunk] = src[:m.start(1)] + f"{local} as {exported}," + src[m.start(1):]
    src = bundle[target]
    known = imports(src)
    alias = next((a for a, ref in known.items() if ref == (chunk, exported)), None)
    if alias is not None:
        return alias
    imp = re.search(r'import\{([^}]*)\}from"\./' + re.escape(chunk) + r'";', src)
    if imp is None:
        raise PatchError(f"{target} doesn't import {chunk}")
    alias = exported if exported.startswith("__qc") else f"__qc_{exported}"
    while alias in known:
        alias += "_"
    bundle[target] = src[:imp.start(1)] + f"{exported} as {alias}," + src[imp.start(1):]
    return alias


# ------------------------------------------------------------------ patterns

TOGGLE_RE = (rf"function ({I})\(e,t=\{{\}}\)\{{if\(!e\.get\(({I}),\{{name:`chatgpt\.quick-chat`\}}\)\.isCapable\)return;"
             rf"let n=e\.get\(({I})\),r=e\.get\(({I})\);if\(r!=null\)\{{({I})\(e,r\.hasConversation\);return\}}")

# The Quick Chat host component, up to and including its useLayoutEffect call.
HOST_RE = (rf"\{{surfaceOwner:(?P<owner>{I})\}}=(?P<props>{I}),(?P<store>{I})={I}\({I}\),(?P<k>{I});"
           rf"(?P<t>{I})\[0\]===Symbol\.for\(`react\.memo_cache_sentinel`\)\?\((?P=k)=\{{name:`chatgpt\.quick-chat`\}},"
           rf"(?P=t)\[0\]=(?P=k)\):(?P=k)=(?P=t)\[0\];let (?P<a>{I})=(?P<useFamily>{I})\((?P<bo>{I}),(?P=k)\)\.isCapable,"
           rf"(?P<o>{I})=(?P<useValue>{I})\((?P<disabled>{I})\),(?P<s>{I})=(?P=a)&&!(?P=o),(?P<c>{I}),(?P<l>{I});"
           rf"if\((?P=t)\[1\]!==(?P=s)\|\|(?P=t)\[2\]!==(?P=store)\?\((?P=c)=\(\)=>\{{(?P=s)\|\|(?P<close>{I})\((?P=store)\)\}},"
           rf"(?P=l)=\[(?P=s),(?P=store)\],(?P=t)\[1\]=(?P=s),(?P=t)\[2\]=(?P=store),(?P=t)\[3\]=(?P=c),(?P=t)\[4\]=(?P=l)\):"
           rf"\((?P=c)=(?P=t)\[3\],(?P=l)=(?P=t)\[4\]\),\(0,(?P<react>{I})\.useLayoutEffect\)\((?P=c),(?P=l)\),")

SUBMIT_RE = (rf"let {I}=(?P<e>{I})\.get\((?P<model>{I}),(?P<t>{I})\);(?P=e)\.set\((?P<submitting>{I}),(?P=t),!0\);try\{{"
             rf"let (?P<hints>{I})={I}\?\?(?P=e)\.get\({I},(?P=t)\),{I}={I}==null\|\|(?P<prompt>{I})\.trim\(\)\.length===0\?"
             rf"void 0:(?P<build>{I})\(\{{messageMetadata:{I},oneTurnDeveloperInstructions:{I},prompt:(?P=prompt),"
             rf"systemHints:(?P=hints)\}}\),{I}=await (?P<send>{I})\((?P=e),\{{conversationId:(?P=t),")

# Pending prompt (null) and pending markdown (false) atoms, followed by three list atoms. 26.1021 inserted a
# derived atom between the first two.
PENDING_RE = (rf"(?P<prompt>{I})=(?P<f>{I})\((?P<s>{I}),e=>null\),(?:{I}={I}\((?P=s),\(e,\{{get:t\}}\)=>[^;]*?\),)?"
              rf"(?P<markdown>{I})=(?P=f)\((?P=s),e=>!1\),{I}=(?P=f)\((?P=s),e=>\[\]\),{I}=(?P=f)\((?P=s),e=>\[\]\),"
              rf"{I}=(?P=f)\((?P=s),e=>\[\]\)")

# The shared selection menu calls useIntl() right after defaulting its terminology prop.
USE_INTL_RE = rf"{I}=({I})===void 0\?`task`:\1,{I}=({I})\(\),\[{I},{I}\]=\(0,{I}\.useState\)"

# The Codex thread selection overlay. Local names are captured rather than assumed, since the minifier
# reassigns them between releases.
OUTER_RE = (rf"function {I}\((?P<props>{I})\)\{{let (?P<t>{I})=\(0,(?P<cache>{I})\.c\)\((?P<size>\d+)\),"
            rf"\{{actions:{I},canOpenSideChat:{I},scopeToThread:{I},targetSelector:(?P<sel>{I})\}}=(?P=props),"
            rf"(?P<store>{I})={I}\({I}\),")


# --------------------------------------------------- Quick Chat internals

def patch_core(bundle):
    core, m = bundle.find(TOGGLE_RE, "quick chat toggle", "chatgpt.quick-chat")
    src = bundle[core]
    n = {}
    n["toggle"], n["Bo"], n["session"], n["show"] = m.group(1), m.group(2), m.group(3), m.group(5)

    m = one(r"function " + re.escape(n["show"]) + r"\(e,t\)\{let n=!e\.get\(([\w$]+)\);e\.set\(\1,!0\),"
            r"e\.set\([\w$]+,n&&!t\?`welcome`:`recent`\),e\.set\(([\w$]+),!0\)\}", src, "quick chat visibility atom")
    n["visible"] = m.group(2)

    m = one(r"function ([\w$]+)\(e,\{conversationId:t=([\w$]+)\(\),contextSourceThread:n=null,projectId:r=null,"
            r"projectName:i=null,preservePreviousSession:a=!1\}=\{\}\)", src, "quick chat session opener")
    n["openSession"], n["newConversationId"] = m.group(1), m.group(2)

    m = one(r"function ([\w$]+)\(e,t,n=\{\}\)\{if\(!e\.get\(" + re.escape(n["Bo"]) +
            r",\{name:`chatgpt\.quick-chat`\}\)\.isCapable\)return;let r=e\.get\(" + re.escape(n["session"]) +
            r"\);r\?\.conversationId!==t\|\|r\.hasConversation\|\|", src, "quick chat conversation-started")
    n["markStarted"] = m.group(1)

    m = one(SUBMIT_RE, src, "ChatGPT submit helper")
    n["modelSettings"], n["submitting"], n["buildMessages"], n["send"] = m["model"], m["submitting"], m["build"], m["send"]
    send_def = one(r"(?:async )?function " + re.escape(n["send"]) + r"\(", src, "send() definition")
    build_def = one(r"function " + re.escape(n["buildMessages"]) + r"\(", src, "buildMessages() definition")

    m = one(PENDING_RE, src, "pending prompt atoms")
    n["pendingPrompt"], n["pendingMarkdown"] = m["prompt"], m["markdown"]
    pending_pos = m.start()

    m = one(r"async function ([\w$]+)\(e,\{isBackgroundSubagentsEnabled:t,sourceThread:n,signal:r\}\)\{let i=\(await "
            r"[\w$]+\(e,\{sourceThread:n,signal:r,isBackgroundSubagentsEnabled:t,markdownLimit:", src,
            "Codex context builder")
    n["codexContext"] = m.group(1)
    context_pos = m.start()

    m = one(r"return n==null\?null:\{threadId:n,hostId:e\.get\(([\w$]+),n\)\}\}", src, "thread host atom")
    n["threadHost"] = m.group(1)

    m = one(r"([\w$]+)=[\w$]+\([\w$]+,\(e,\{get:t\}\)=>e==null\?null:[\w$]+\(e\)\?t\([\w$]+,e\):[\w$]+\(e\)\)",
            src, "server conversation id atom")
    n["serverId"] = m.group(1)
    server_pos = m.start()

    m = one(r"([\w$]+)=[\w$]+\([\w$]+,\(e,\{get:t\}\)=>\{let n=[\w$]+\(t,e\);if\(n==null\|\|!"
            r"[\w$]+\(t\([\w$]+,e\)\)\)return!1;let r=t\([\w$]+,n\);return r==null\|\|t\([\w$]+,n\)!==r\}\)",
            src, "streaming atom")
    n["streaming"] = m.group(1)

    m = one(r"function ([\w$]+)\(e,\{includeImages:t=!1,includeCollapsedContent:n=!1\}=\{\}\)\{let r=e\."
            r"ownerDocument\.createRange\(\);return r\.selectNodeContents\(e\)", src, "plain text extractor")
    n["plainText"] = m.group(1)
    plain_pos = m.start()

    toasts = re.findall(r"\.get\(([\w$]+)\)\.danger\(", src)
    if not toasts:
        raise PatchError("toast atom not found")
    n["toast"] = max(set(toasts), key=toasts.count)

    session_pos = one(r"[,(]" + re.escape(n["session"]) + r"=", src, "session atom").start()
    submitting_pos = one(r"[,(]" + re.escape(n["submitting"]) + r"=", src, "submitting atom").start()
    inits = sorted({
        init_containing(src, session_pos),
        init_containing(src, submitting_pos),
        init_containing(src, server_pos),
        init_containing(src, pending_pos),
        init_after(src, send_def.end()),
        init_after(src, build_def.end()),
        init_after(src, context_pos),
        init_after(src, plain_pos),
    })

    # The host component's capability gate supplies the hooks for __qcCan and the panel's close function.
    host, h = bundle.find(HOST_RE, "quick chat host", "surfaceOwner")
    if canonical(bundle, host, h["bo"]) != canonical(bundle, core, n["Bo"]):
        raise PatchError("quick chat host: capability atom doesn't match the toggle's")
    for key in ("useFamily", "useValue", "disabled", "close"):
        n[key] = bind(bundle, core, *canonical(bundle, host, h[key]))

    js = r"""
;/* codex-more-details-restore: port of 26.1002.52244 selected-text Quick Chat */
var __qcPend=[],__qcCapKey={name:`chatgpt.quick-chat`};
function __qcCan(){return USEFAMILY(BO,__qcCapKey).isCapable&&!USEVALUE(DISABLED)}
function __qcTrunc(e,t){let L=4e4,R=`\n\n… Codex conversation truncated …\n\n`,Z=`\n\n… selected text shown above …\n\n`;if(e.length<=L)return e;let n=e.indexOf(t);if(n===-1){let r=L-R.length,i=Math.floor(r/2),a=r-i;return`${e.slice(0,i)}${R}${e.slice(-a)}`}let r=n+t.length;return`${e.slice(Math.max(0,n-19983),n)}${Z}${e.slice(r,r+19984)}`}
function __qcBusy(e,t){return e(SUBMITTING,t)||e(SERVERID,t)==null&&e(STREAMING,t)}
async function __qcPrep(e,t,n){let r=t.selectedText.trim();if(r.length===0)return null;let i=__qcTrunc(t.selectedMessageText.trim()||r,r),o=[...await CODEXCONTEXT(e,{isBackgroundSubagentsEnabled:t.isBackgroundSubagentsEnabled,sourceThread:{threadId:t.sourceConversationId,hostId:e.get(THREADHOST,t.sourceConversationId)}}),`The user is referring to this in particular:\n${r}\n\n---\n\nThe selection appears in the following source message. Prioritize this source message over the broader conversation context:\n\n${i}`],a=e.get(MODELSETTINGS,n);return{model:a.slug,thinkingEffort:a.thinkingEffort,userCompletionMessages:BUILDMESSAGES({extraDeveloperInstructions:o,messageMetadata:{is_visually_hidden_from_conversation:!0,targeted_reply:r,targeted_reply_label:t.targetedReplyLabel},prompt:t.prompt})}}
async function __qcStart(e,t){if(!e.get(BO,{name:`chatgpt.quick-chat`}).isCapable)return;let n=e.get(SESSION),i=n?.selectedTextSourceConversationId===t.sourceConversationId?n.conversationId:__qcPend.find(e=>e.sourceConversationId===t.sourceConversationId)?.conversationId??NEWID(),a={conversationId:i,sourceConversationId:t.sourceConversationId};__qcPend=[...__qcPend,a];let o;try{o=await __qcPrep(e,t,i)}finally{__qcPend=__qcPend.filter(e=>e!==a)}if(o==null)return;for(;__qcBusy(e.get,i);)await e.when(({get:e})=>!__qcBusy(e,i));if(!e.get(BO,{name:`chatgpt.quick-chat`}).isCapable)return;let s=e.get(SESSION);e.set(SUBMITTING,i,!0),e.set(PENDINGMARKDOWN,i,!1),e.set(PENDINGPROMPT,i,t.prompt),s?.conversationId===i?e.set(SESSION,{...s,selectedTextSourceConversationId:t.sourceConversationId}):(OPENSESSION(e,{conversationId:i}),e.set(SESSION,{...e.get(SESSION),selectedTextSourceConversationId:t.sourceConversationId})),SHOW(e,!0),MARKSTARTED(e,i);try{await SEND(e,{conversationId:i,model:o.model,prompt:t.prompt,thinkingEffort:o.thinkingEffort,userCompletionMessages:o.userCompletionMessages})}finally{e.set(PENDINGPROMPT,i,null),e.set(SUBMITTING,i,!1)}}
var __qcKeyScopes=[];
function __qcBindKeys(e,n,s){if(n!=null||!s)return;__qcKeyScopes.push(e);window.__qcKeysBound||(window.__qcKeysBound=!0,window.addEventListener(`keydown`,__qcOnKey,!0));return()=>{let t=__qcKeyScopes.lastIndexOf(e);t>=0&&__qcKeyScopes.splice(t,1)}}
function __qcOnKey(t){if(t.repeat||t.code!==`Space`||t.ctrlKey)return;let m=t.metaKey&&t.shiftKey&&!t.altKey,o=t.altKey&&!t.metaKey&&!t.shiftKey;if(!m&&!o)return;let e=__qcKeyScopes[__qcKeyScopes.length-1];if(e==null)return;t.preventDefault(),t.stopPropagation();try{e.get(VISIBLE)&&e.get(SESSION)!=null?CLOSE(e):TOGGLE(e,{})}catch(n){console.error(`[more-details] Quick Chat shortcut failed`,n)}}
function __qcMDRun(e,{selectedText:s,target:l,sourceConversationId:c,intl:I}){if(c==null)return;try{INITS}catch(e){console.warn(`[more-details] init`,e)}let m=I.formatMessage({id:`selectedTextOverlay.quickChatPrompt`,defaultMessage:`Tell me more about this`,description:`Prompt sent in Quick Chat when asking about selected text`}),b=I.formatMessage({id:`chatGptConversation.targetedReply.selectedText`,defaultMessage:`Selected text`,description:`Label shown above a Quick Chat message that replies to text selected in a Codex conversation`}),x=null;try{x=l==null?null:PLAINTEXT(l)}catch{}__qcStart(e,{isBackgroundSubagentsEnabled:!0,prompt:m,selectedMessageText:x?.plainText??l?.innerText??``,selectedText:s,sourceConversationId:c,targetedReplyLabel:b}).catch(n=>{console.error(`[Composer] selected text Quick Chat failed`,n);try{e.get(TOAST).danger(I.formatMessage({id:`selectedTextOverlay.quickChatError`,defaultMessage:`Failed to start chat`,description:`Toast shown when asking about selected text in chat fails`}),{errorAnalytics:{toastId:`selectedTextOverlay.quickChatError`}})}catch{}})}
"""
    subs = {
        "USEFAMILY": n["useFamily"], "USEVALUE": n["useValue"], "DISABLED": n["disabled"], "BO": n["Bo"],
        "SUBMITTING": n["submitting"], "SERVERID": n["serverId"], "STREAMING": n["streaming"],
        "CODEXCONTEXT": n["codexContext"], "THREADHOST": n["threadHost"], "MODELSETTINGS": n["modelSettings"],
        "BUILDMESSAGES": n["buildMessages"], "SESSION": n["session"], "NEWID": n["newConversationId"],
        "PENDINGMARKDOWN": n["pendingMarkdown"], "PENDINGPROMPT": n["pendingPrompt"],
        "OPENSESSION": n["openSession"], "SHOW": n["show"], "MARKSTARTED": n["markStarted"], "SEND": n["send"],
        "PLAINTEXT": n["plainText"], "TOAST": n["toast"], "VISIBLE": n["visible"], "CLOSE": n["close"],
        "TOGGLE": n["toggle"], "INITS": ",".join(f"{i}()" for i in inits),
    }
    js = re.sub(r"\b(" + "|".join(sorted(subs, key=len, reverse=True)) + r")\b", lambda m: subs[m.group(1)], js)

    src = bundle[core]
    exp, _ = exports(src)
    bundle[core] = (src[:exp.start()] + js + "\n" + src[exp.start():exp.start(1)]
                    + "__qcMDRun as __qcMDRun,__qcCan as __qcCan,__qcBindKeys as __qcBindKeys," + src[exp.start(1):])

    # Register the shortcut while the host is mounted.
    bind_keys = bind(bundle, host, core, "__qcBindKeys")
    src = bundle[host]
    h = one(HOST_RE, src, "quick chat host")
    args = f"{h['store']},{h['owner']},{h['s']}"
    bundle[host] = src[:h.end()] + f"(0,{h['react']}.useEffect)(()=>{bind_keys}({args}),[{args}])," + src[h.end():]
    return core, n


# --------------------------------------------------- selection overlays

def patch_overlays(bundle, core):
    menu, m = bundle.find(USE_INTL_RE, "useIntl in selection menu", "conversationTerminology")
    chunk, _ = bundle.find(OUTER_RE, "Codex selection overlay", "canOpenSideChat")
    use_intl = bind(bundle, chunk, *canonical(bundle, menu, m.group(2)))
    md_run = bind(bundle, chunk, core, "__qcMDRun")
    can = bind(bundle, chunk, core, "__qcCan")

    # Codex thread overlay: compute an onMoreDetails handler and pass it down.
    src = bundle[chunk]
    q = one(OUTER_RE, src, "Codex selection overlay")
    t, cache, size, sel, store = q["t"], q["cache"], int(q["size"]), q["sel"], q["store"]
    T, S, A = re.escape(t), re.escape(store), re.escape(sel)
    q_end = src.index("}function ", q.end())
    body = src[q.start():q_end]
    reacts = set(re.findall(r"\(0,([\w$]+)\.useContext\)", body))
    if len(reacts) != 1:
        raise PatchError(f"React namespace: expected 1 name, found {sorted(reacts)}")
    react = reacts.pop()
    thread_id = one(rf"{T}\[2\]==={S}\?(?P<d>{I})={T}\[3\]:\((?P=d)=(?P<id>{I})\({S}\),{T}\[2\]={S},{T}\[3\]=(?P=d)\)",
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
        f"__qi={use_intl}(),__qc={can}(),__md=(0,{react}.useMemo)(()=>__qc?(__s,__l)=>{md_run}({store},"
        f"{{selectedText:__s,target:__l,sourceConversationId:{thread_id}({store}),intl:__qi}}):void 0,[{store},__qi,__qc]),"
    ) + body[cut:]
    body = body.replace(f"(0,{cache}.c)({size})", f"(0,{cache}.c)({size + 1})", 1)
    bundle[chunk] = src[:q.start()] + body + src[q_end:]

    # Inner overlay: accept onMoreDetails and hand it to the shared selection menu.
    chunk, inner = canonical(bundle, chunk, inner)
    src = bundle[chunk]
    e = one(rf"function {re.escape(inner)}\((?P<props>{I})\)\{{let (?P<t>{I})=\(0,(?P<cache>{I})\.c\)\((?P<size>\d+)\),"
            rf"\{{onAddResponseTextAnnotation:(?P<n>{I}),onOpenSideChat:(?P<r>{I}),targetContainerRefs:(?P<i>{I}),"
            rf"targetSelector:(?P<a>{I})\}}=(?P=props),(?P<o>{I})=(?P<zoom>{I})\(\);if\((?P=n)==null&&(?P=r)==null\)"
            rf"return null;let (?P<s>{I});(?P=t)\[0\]!==(?P=n)\|\|(?P=t)\[1\]!==(?P=r)\|\|(?P=t)\[2\]!==(?P=o)\?\((?P=s)=",
            src, "inner overlay")
    props, t, n, r, o, s, size = e["props"], e["t"], e["n"], e["r"], e["o"], e["s"], int(e["size"])
    e_end = src.index("}function ", e.end())
    body = src[e.start():e_end]
    target = one(rf"\{{portalTarget:{I},rect:{I},selectedText:{I},selectionRange:{I},target:({I})\}}={I}", body,
                 "selection target").group(1)
    body = body.replace(e.group(0), (
        f"function {inner}({props}){{let {t}=(0,{e['cache']}.c)({size + 1}),{{onAddResponseTextAnnotation:{n},"
        f"onMoreDetails:__md,onOpenSideChat:{r},targetContainerRefs:{e['i']},targetSelector:{e['a']}}}={props},"
        f"{o}={e['zoom']}();if({n}==null&&{r}==null&&__md==null)return null;let {s};"
        f"{t}[0]!=={n}||{t}[1]!=={r}||{t}[2]!=={o}||{t}[{size}]!==__md?({s}="), 1)
    T, N, R, O, S = (re.escape(x) for x in (t, n, r, o, s))
    mp = one(rf"\}},onOpenSideChat:{R}\}}\)\}}\)\}},{T}\[0\]={N},{T}\[1\]={R},{T}\[2\]={O},{T}\[3\]={S}\):{S}={T}\[3\]",
             body, "inner overlay menu props")
    body = body[:mp.start()] + (
        f"}},onMoreDetails:__md==null?void 0:__e=>__md(__e,{target}),onOpenSideChat:{r}}})}})}},"
        f"{t}[0]={n},{t}[1]={r},{t}[2]={o},{t}[{size}]=__md,{t}[3]={s}):{s}={t}[3]") + body[mp.end():]
    bundle[chunk] = src[:e.start()] + body + src[e_end:]


# ------------------------------------------------------------------ main

def node_check(name, text):
    node = shutil.which("node")
    if node is None:
        raise PatchError(f"node not found; cannot validate {name}")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, name.replace(".js", ".mjs"))
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        r = subprocess.run([node, "--check", p], capture_output=True, text=True)
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
    bundle = Bundle(os.path.join(app, "resources/app.asar" if os.name == "nt" else "Contents/Resources/app.asar"))
    core, names = patch_core(bundle)
    patch_overlays(bundle, core)
    if os.name == "nt":
        # Keep Alt+Space available for the Windows system menu.
        bundle[core] = bundle[core].replace(
            "t.code!==`Space`||t.ctrlKey)return;let m=t.metaKey&&t.shiftKey&&!t.altKey,o=t.altKey&&!t.metaKey&&!t.shiftKey",
            "t.code!==`Space`||t.metaKey)return;let m=t.ctrlKey&&t.shiftKey&&!t.altKey,o=!1",
            1,
        )
    print("  resolved:", ", ".join(f"{k}={v}" for k, v in sorted(names.items())))
    for name in bundle.changed:
        node_check(name, bundle[name])
    return {name: bundle[name] for name in bundle.changed}


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
