#!/usr/bin/env python3
"""Restore the "More details" -> Quick Chat action in the Codex selection menu.

ChatGPT/Codex desktop 26.1007.x dropped the `onMoreDetails` wiring from the
Codex thread selection overlay and deleted the "start Quick Chat from selected
text" helper (see openai/codex#52483). This script ports the 26.1002.52244
implementation back:

  * app-initial-*.js: re-adds the selected-text Quick Chat starter (sends
    "Tell me more about this" with the selection + source message as a
    targeted reply, exactly like 26.1002) and a capability hook.
  * app-primary-*.js: passes an `onMoreDetails` handler from the Codex thread
    overlay into the shared selection menu, which still renders the button.
  * app-initial-*.js also gets a Quick Chat shortcut: Cmd-Shift-Space (or
    Option-Space, if the global "Toggle Mini" shortcut is cleared) toggles the
    floating Quick Chat panel while ChatGPT is frontmost. 26.1007 removed the
    standalone Quick Chat window from the main process, so this cannot be global.

Both bundles are read from ChatGPT.app's app.asar without modifying it.
Minified identifiers are located by regex rather than hard-coded, and the
patched output must pass `node --check`. If anything doesn't match (for example
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


def init_containing(text, assignment_pos):
    """Name of the rolldown lazy-init function whose body contains assignment_pos."""
    best = None
    for m in re.finditer(r"function ([\w$]+)\(\)\{return\(\1=e\(\(\(\)=>\{", text):
        if m.start() > assignment_pos:
            break
        best = m
    if best is None:
        raise PatchError("could not find module init")
    return best.group(1)


def init_after(text, pos):
    m = re.compile(r"function ([\w$]+)\(\)\{return\(\1=e\(\(\(\)=>\{").search(text, pos)
    if m is None:
        raise PatchError("could not find module init")
    return m.group(1)


def exports(text):
    m = re.search(r"export\{([^}]*)\};?\s*(//# sourceMappingURL=[^\n]*)?\s*$", text)
    if m is None:
        raise PatchError("export list not found")
    names = {}
    for part in m.group(1).split(","):
        bits = part.strip().split(" as ")
        names[bits[0]] = bits[-1]
    return m, names


# ------------------------------------------------------- app-initial patch

def patch_initial(src):
    if MARKER in src:
        raise PatchError("app-initial already patched")
    n = {}

    m = one(r"function ([\w$]+)\(e,t=\{\}\)\{if\(!e\.get\(([\w$]+),\{name:`chatgpt\.quick-chat`\}\)\.isCapable\)return;"
            r"let n=e\.get\(([\w$]+)\),r=e\.get\(([\w$]+)\);if\(r!=null\)\{([\w$]+)\(e,r\.hasConversation\);return\}",
            src, "quick chat toggle")
    n["toggle"], n["Bo"], n["session"], n["show"] = m.group(1), m.group(2), m.group(3), m.group(5)

    m = one(r"function " + re.escape(n["show"]) + r"\(e,t\)\{let n=!e\.get\(([\w$]+)\);e\.set\(\1,!0\),"
            r"e\.set\([\w$]+,n&&!t\?`welcome`:`recent`\),e\.set\(([\w$]+),!0\)\}", src, "quick chat visibility atom")
    n["visible"] = m.group(2)

    host = one(r"\{name:`chatgpt\.quick-chat`\},t\[0\]=i\):i=t\[0\];let a=[\w$]+\([\w$]+,i\)\.isCapable,o=[\w$]+\([\w$]+\),"
               r"s=a&&!o,c,l;if\(t\[1\]!==s\|\|t\[2\]!==r\?\(c=\(\)=>\{s\|\|([\w$]+)\(r\)\},l=\[s,r\],t\[1\]=s,"
               r"t\[2\]=r,t\[3\]=c,t\[4\]=l\):\(c=t\[3\],l=t\[4\]\),\(0,([\w$]+)\.useLayoutEffect\)\(c,l\),", src,
               "quick chat host")
    if "{surfaceOwner:n}=e,r=" not in src[host.start() - 120:host.start()]:
        raise PatchError("quick chat host: unexpected props")
    n["close"], host_react = host.group(1), host.group(2)

    m = one(r"function ([\w$]+)\(e,\{conversationId:t=([\w$]+)\(\),contextSourceThread:n=null,projectId:r=null,"
            r"projectName:i=null,preservePreviousSession:a=!1\}=\{\}\)", src, "quick chat session opener")
    n["openSession"], n["newConversationId"] = m.group(1), m.group(2)

    m = one(r"function ([\w$]+)\(e,t,n=\{\}\)\{if\(!e\.get\(" + re.escape(n["Bo"]) +
            r",\{name:`chatgpt\.quick-chat`\}\)\.isCapable\)return;let r=e\.get\(" + re.escape(n["session"]) +
            r"\);r\?\.conversationId!==t\|\|r\.hasConversation\|\|", src, "quick chat conversation-started")
    n["markStarted"] = m.group(1)

    m = one(r"let p=e\.get\(([\w$]+),t\);e\.set\(([\w$]+),t,!0\);try\{let f=u\?\?e\.get\([\w$]+,t\),"
            r"m=o==null\|\|c\.trim\(\)\.length===0\?void 0:([\w$]+)\(\{messageMetadata:i,oneTurnDeveloperInstructions:a,"
            r"prompt:c,systemHints:f\}\),h=await ([\w$]+)\(e,\{conversationId:t,", src, "ChatGPT submit helper")
    n["modelSettings"], n["submitting"], n["buildMessages"], n["send"] = m.groups()
    send_def = one(r"(?:async )?function " + re.escape(n["send"]) + r"\(", src, "send() definition")
    build_def = one(r"function " + re.escape(n["buildMessages"]) + r"\(", src, "buildMessages() definition")

    m = one(r"([\w$]+)=([\w$]+)\(([\w$]+),e=>null\),([\w$]+)=\2\(\3,e=>!1\),[\w$]+=\2\(\3,e=>\[\]\),"
            r"[\w$]+=\2\(\3,e=>\[\]\),[\w$]+=\2\(\3,e=>\[\]\),[\w$]+=\2\(\3,\(e,\{scope:t\}\)=>\[\]", src,
            "pending prompt atoms")
    n["pendingPrompt"], n["pendingMarkdown"] = m.group(1), m.group(4)

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

    m = one(r"\{name:`chatgpt\.quick-chat`\},t\[0\]=i\):i=t\[0\];let a=([\w$]+)\(" + re.escape(n["Bo"]) +
            r",i\)\.isCapable,o=([\w$]+)\(([\w$]+)\),s=a&&!o", src, "quick chat host gate")
    n["useFamily"], n["useValue"], n["disabled"] = m.groups()

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
        init_after(src, send_def.end()),
        init_after(src, build_def.end()),
        init_after(src, context_pos),
        init_after(src, plain_pos),
    })

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

    hook = f"(0,{host_react}.useLayoutEffect)(c,l),"
    src = src[:host.end() - len(hook)] + hook + f"(0,{host_react}.useEffect)(()=>__qcBindKeys(r,n,s),[r,n,s])," + src[host.end():]

    exp_match, _ = exports(src)
    head, tail = src[:exp_match.start()], src[exp_match.start():]
    tail = tail.replace("export{", "export{__qcMDRun as __qcMDRun,__qcCan as __qcCan,", 1)
    return head + js + "\n" + tail, n


# ------------------------------------------------------- app-primary patch

def patch_primary(src, initial_name):
    if MARKER in src:
        raise PatchError("app-primary already patched")

    imp = one(r'import\{([^}]*)\}from"\./' + re.escape(initial_name) + r'";', src, "app-initial import")
    src = src[:imp.start(1)] + "__qcMDRun as __qcMDRun,__qcCan as __qcCan," + src[imp.start(1):]

    m = one(r"d=n===void 0\?`task`:n,f=([\w$]+)\(\),\[p,m\]=", src, "useIntl in selection overlay")
    use_intl = m.group(1)

    # Codex thread overlay: compute an onMoreDetails handler and pass it down.
    q = one(r"function ([\w$]+)\(e\)\{let t=\(0,([\w$]+)\.c\)\((\d+)\),\{actions:n,canOpenSideChat:r,"
            r"scopeToThread:i,targetSelector:a\}=e,o=([\w$]+)\(([\w$]+)\),", src, "Codex selection overlay")
    q_name, q_cache, q_size = q.group(1), q.group(2), int(q.group(3))
    q_end = src.index("}function ", q.end())
    body = src[q.start():q_end]
    reacts = set(re.findall(r"\(0,([\w$]+)\.useContext\)", body))
    if len(reacts) != 1:
        raise PatchError(f"React namespace: expected 1 name, found {sorted(reacts)}")
    react = reacts.pop()
    thread_id = one(r"t\[2\]===o\?d=t\[3\]:\(d=([\w$]+)\(o\),t\[2\]=o,t\[3\]=d\)", body, "thread id").group(1)
    body = body.replace(f"(0,{q_cache}.c)({q_size})", f"(0,{q_cache}.c)({q_size + 1})", 1)
    body = body.replace(
        q.group(0)[len(f"function {q_name}(e){{let t=(0,{q_cache}.c)({q_size}),"):],
        q.group(0)[len(f"function {q_name}(e){{let t=(0,{q_cache}.c)({q_size}),"):]
        + f"__qi={use_intl}(),__qc=__qcCan(),__md=(0,{react}.useMemo)(()=>__qc?(s,l)=>__qcMDRun(o,"
          f"{{selectedText:s,target:l,sourceConversationId:{thread_id}(o),intl:__qi}}):void 0,[o,__qi,__qc]),",
        1)
    jm = one(r"t\[13\]!==h\|\|t\[14\]!==g\|\|t\[15\]!==_\|\|t\[16\]!==a\?\(v=\(0,([\w$]+)\.jsx\)\(([\w$]+),"
             r"\{onAddResponseTextAnnotation:h,onOpenSideChat:g,targetContainerRefs:_,targetSelector:a\}\),"
             r"t\[13\]=h,t\[14\]=g,t\[15\]=_,t\[16\]=a,t\[17\]=v\)", body, "overlay render")
    inner = jm.group(2)
    body = body[:jm.start()] + (
        f"t[13]!==h||t[14]!==g||t[15]!==_||t[16]!==a||t[{q_size}]!==__md?(v=(0,{jm.group(1)}.jsx)({inner},"
        f"{{onAddResponseTextAnnotation:h,onMoreDetails:__md,onOpenSideChat:g,targetContainerRefs:_,targetSelector:a}}),"
        f"t[13]=h,t[14]=g,t[15]=_,t[16]=a,t[{q_size}]=__md,t[17]=v)") + body[jm.end():]
    src = src[:q.start()] + body + src[q_end:]

    # Inner overlay: accept onMoreDetails and hand it to the shared selection menu.
    e = one(r"function " + re.escape(inner) + r"\(e\)\{let t=\(0,([\w$]+)\.c\)\((\d+)\),\{onAddResponseTextAnnotation:n,"
            r"onOpenSideChat:r,targetContainerRefs:i,targetSelector:a\}=e,o=([\w$]+)\(\);if\(n==null&&r==null\)"
            r"return null;let s;t\[0\]!==n\|\|t\[1\]!==r\|\|t\[2\]!==o\?\(s=", src, "inner overlay")
    e_cache, e_size, e_zoom = e.group(1), int(e.group(2)), e.group(3)
    e_end = src.index("}function ", e.end())
    body = src[e.start():e_end]
    target = one(r"\{portalTarget:i,rect:a,selectedText:s,selectionRange:c,target:([\w$]+)\}=e", body,
                 "selection target").group(1)
    body = body.replace(e.group(0), (
        f"function {inner}(e){{let t=(0,{e_cache}.c)({e_size + 1}),{{onAddResponseTextAnnotation:n,onMoreDetails:__md,"
        f"onOpenSideChat:r,targetContainerRefs:i,targetSelector:a}}=e,o={e_zoom}();if(n==null&&r==null&&__md==null)"
        f"return null;let s;t[0]!==n||t[1]!==r||t[2]!==o||t[{e_size}]!==__md?(s="), 1)
    old = "},onOpenSideChat:r})})},t[0]=n,t[1]=r,t[2]=o,t[3]=s):s=t[3]"
    if body.count(old) != 1:
        raise PatchError("inner overlay menu props not found")
    body = body.replace(old, (
        f"}},onMoreDetails:__md==null?void 0:e=>__md(e,{target}),onOpenSideChat:r}})}})}},"
        f"t[0]=n,t[1]=r,t[2]=o,t[{e_size}]=__md,t[3]=s):s=t[3]"), 1)
    src = src[:e.start()] + body + src[e_end:]
    return src


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
    asar = os.path.join(app, "resources/app.asar" if os.name == "nt" else "Contents/Resources/app.asar")
    header, raw, base = read_asar(asar)
    files = dict(walk(header))
    pick = lambda prefix: one(r"(?m)^webview/assets/" + prefix + r"-[0-9a-f]+\.js$", "\n".join(files), prefix).group(0)
    initial_path, primary_path = pick("app-initial"), pick("app-primary")
    initial = read_file(asar, base, files[initial_path]).decode("utf-8")
    primary = read_file(asar, base, files[primary_path]).decode("utf-8")

    new_initial, names = patch_initial(initial)
    if os.name == "nt":
        # Keep Alt+Space available for the Windows system menu.
        new_initial = new_initial.replace(
            "t.code!==`Space`||t.ctrlKey)return;let m=t.metaKey&&t.shiftKey&&!t.altKey,o=t.altKey&&!t.metaKey&&!t.shiftKey",
            "t.code!==`Space`||t.metaKey)return;let m=t.ctrlKey&&t.shiftKey&&!t.altKey,o=!1",
            1,
        )
    new_primary = patch_primary(primary, os.path.basename(initial_path))
    print("  resolved:", ", ".join(f"{k}={v}" for k, v in sorted(names.items())))
    node_check(os.path.basename(initial_path), new_initial)
    node_check(os.path.basename(primary_path), new_primary)
    return {os.path.basename(initial_path): new_initial, os.path.basename(primary_path): new_primary}


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
