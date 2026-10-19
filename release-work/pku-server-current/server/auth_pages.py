"""Minimal HTML landings for email verify / password reset (no assets)."""

from __future__ import annotations

import html


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<link rel="icon" type="image/png" href="/site/icon.png">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body {{ margin:0; font-family:"PingFang SC","Microsoft YaHei","Noto Sans SC",system-ui,sans-serif;
  color:#203531; background:#f4f7f3; line-height:1.55; }}
main {{ max-width:440px; margin:12vh auto; padding:28px 24px; background:#fffefa;
  border:1px solid #dfe8e2; border-radius:18px; }}
h1 {{ margin:0 0 10px; font-size:22px; letter-spacing:-.02em; }}
p {{ margin:0 0 12px; color:#4a5f59; }}
.ok {{ color:#2e7761; }}
.err {{ color:#a15252; }}
label {{ display:block; font-size:13px; margin:14px 0 6px; }}
input {{ width:100%; box-sizing:border-box; padding:10px 12px; border:1px solid #dfe8e2;
  border-radius:10px; font:inherit; }}
button, .button-link {{ display:block; box-sizing:border-box; text-align:center; text-decoration:none; margin-top:16px; width:100%; padding:11px 14px; border:0; border-radius:10px;
  background:#205647; color:#fff; font:inherit; font-weight:600; cursor:pointer; }}
.note {{ margin-top:18px; font-size:12px; color:#8a9a94; }}
</style>
</head>
<body><main>{body}</main></body>
</html>
"""


DOWNLOAD_CTA = '<a class="button-link" href="/#download">前往下载桌面客户端（Windows / macOS）</a>'


def verify_result_page(*, ok: bool, message: str) -> str:
    cls = "ok" if ok else "err"
    title = "邮箱已验证" if ok else "验证没有成功"
    return _page(
        title,
        f"<h1>{html.escape(title)}</h1>"
        f'<p class="{cls}">{html.escape(message)}</p>'
        + (DOWNLOAD_CTA if ok else '') +
        '<p class="note">PKU All in One</p>',
    )


def reset_form_page(*, token: str, error: str = "") -> str:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return _page(
        "重置密码",
        "<h1>设置新密码</h1>"
        "<p>请输入至少 8 位的新密码。提交后，其他设备上的登录会失效。</p>"
        f"{err}"
        '<form method="post" action="/reset">'
        f'<input type="hidden" name="token" value="{html.escape(token)}">'
        '<label for="password">新密码</label>'
        '<input id="password" name="password" type="password" minlength="8" required autocomplete="new-password">'
        '<label for="password2">再输入一次</label>'
        '<input id="password2" name="password2" type="password" minlength="8" required autocomplete="new-password">'
        "<button type=\"submit\">确认重置</button>"
        "</form>"
        '<p class="note">PKU All in One</p>',
    )


def reset_result_page(*, ok: bool, message: str) -> str:
    cls = "ok" if ok else "err"
    title = "密码已重置" if ok else "重置没有成功"
    return _page(
        title,
        f"<h1>{html.escape(title)}</h1>"
        f'<p class="{cls}">{html.escape(message)}</p>'
        '<p class="note">请回到客户端重新登录。</p>',
    )


def invite_landing_page(
    *, invite_code: str, inviter_label: str = "", mode: str = "register",
    email: str = "", error: str = "", ok_message: str = "",
) -> str:
    # Public invite links must never expose an inviter's full email or legacy label.
    local, sep, domain = inviter_label.partition("@")
    masked = local[:1] + "***@" + domain if sep and domain else "北大同学"
    banner = (
        f'<p class="err invite-banner" role="alert">{html.escape(error)}</p>' if error else
        f'<p class="ok invite-banner" role="status">{html.escape(ok_message)}</p>' if ok_message else ""
    )
    if ok_message:
        title = "注册成功，邀请已绑定！" if mode == "register" else "邀请已成功绑定！"
        next_steps = ok_message.removeprefix(title).lstrip()
        return _page(
            title,
            f'<section class="invite-success"><h1 class="ok">{title}</h1>'
            f'<p role="status">{html.escape(next_steps)}</p>'
            '<ol><li>验证邮箱：请查收验证邮件并点击链接；已验证的账号可直接继续。</li>'
            '<li>下载桌面客户端，使用同一邮箱登录。</li>'
            '<li>连接北大教学网，新学号验证成功后邀请奖励自动到账。</li></ol>'
            + DOWNLOAD_CTA + '</section>',
        )
    form = (
        f'<form method="post" action="/invite/{html.escape(invite_code, quote=True)}/claim">'
        '<label for="invite-mode">选择账号方式</label>'
        '<select id="invite-mode" name="mode">'
        f'<option value="register"{" selected" if mode == "register" else ""}>新用户网页直接注册并绑定邀请</option>'
        f'<option value="login"{" selected" if mode != "register" else ""}>已有账号直接绑定邀请</option>'
        '</select><label for="email">邮箱</label>'
        f'<input id="email" name="email" type="email" required autocomplete="email" value="{html.escape(email, quote=True)}">'
        '<label for="password">密码（至少 8 位）</label>'
        f'<input id="password" name="password" type="password" minlength="8" required autocomplete="{"new-password" if mode == "register" else "current-password"}">'
        f'<div id="password2-field"{" hidden" if mode != "register" else ""}><label for="password2">确认密码</label>'
        f'<input id="password2" name="password2" type="password" minlength="8" autocomplete="new-password"{" required" if mode == "register" else " disabled"}></div>'
        f'<button id="invite-submit" type="submit">{"注册并绑定邀请" if mode == "register" else "登录并绑定邀请"}</button>'
        '</form><p class="note">新用户可直接在本页注册。注册后请查收验证邮件；随后在客户端连接北大教学网，奖励会自动到账。</p>'
    )
    return _page(
        "受邀加入 PKU All in One",
        "<h1>受邀加入 PKU All in One</h1>"
        f'<p>来自 <strong>{html.escape(masked)}</strong> 的邀请</p>'
        '<p>绑定新的北大教学网学号后，邀请人与受邀人各得 <strong>1 个月 Lite 普通转写额度，每周 25 小时自动滚存</strong>。</p>'
        '<h2 style="font-size:18px;margin:22px 0 8px">网页直接注册并绑定邀请</h2>'
        f'{banner}{form}'
        '<aside class="invite-desktop-hint"><span>已安装客户端？邀请码 </span>'
        f'<code id="invite-code">{html.escape(invite_code)}</code> '
        '<button id="copy-invite-code" type="button">复制</button>'
        '<small id="copy-status" role="status" aria-live="polite"></small></aside>'
        + (DOWNLOAD_CTA if ok_message else '') +
        '<style>main{margin:5vh auto}select{width:100%;padding:10px;border:1px solid #dfe8e2;border-radius:10px;font:inherit;background:#fff}'
        'code{display:inline-block;padding:5px 10px;background:#eef4ef;border-radius:8px;font-weight:700;letter-spacing:.04em}'
        '.invite-desktop-hint{margin-top:20px;padding-top:14px;border-top:1px solid #e3eae4;font-size:12px;color:#73827b}'
        '#copy-invite-code{display:inline-block;width:auto;margin:0;padding:3px 7px;background:transparent;color:#52695d;font-size:12px;font-weight:500}'
        '#copy-status{display:block;margin-top:4px}.invite-banner{padding:12px;border:1px solid currentColor;border-radius:10px}'
        '@media(max-width:540px){main{margin:16px 12px;padding:24px 18px}}</style>'
        '<script>document.getElementById("copy-invite-code").addEventListener("click",async function(){'
        'var code=document.getElementById("invite-code").textContent;'
        'function fallback(){var input=document.createElement("textarea");input.value=code;document.body.appendChild(input);input.select();'
        'try{if(!document.execCommand("copy"))throw new Error("copy")}finally{input.remove()}}'
        'try{if(navigator.clipboard&&navigator.clipboard.writeText){try{await navigator.clipboard.writeText(code)}catch(e){fallback()}}'
        'else{fallback()}'
        'document.getElementById("copy-status").textContent="邀请码已复制"}'
        'catch(e){document.getElementById("copy-status").textContent="复制失败，请长按上方邀请码复制"}});'
        'var mode=document.getElementById("invite-mode");if(mode){mode.addEventListener("change",function(){'
        'document.getElementById("invite-submit").textContent=mode.value==="register"?"注册并绑定邀请":"登录并绑定邀请";'
        'document.getElementById("password").autocomplete=mode.value==="register"?"new-password":"current-password";'
        'document.getElementById("password2-field").hidden=mode.value!=="register";'
        'var confirm=document.getElementById("password2");confirm.disabled=mode.value!=="register";confirm.required=!confirm.disabled;confirm.setCustomValidity("")});'
        'var password=document.getElementById("password"),confirm=document.getElementById("password2");'
        'function validate(){confirm.setCustomValidity(mode.value==="register"&&password.value!==confirm.value?"两次输入的密码不一致，请重新输入。":"")}'
        'password.addEventListener("input",validate);confirm.addEventListener("input",validate);'
        'mode.form.addEventListener("submit",function(event){validate();if(!mode.form.reportValidity())event.preventDefault()})}</script>',
    )


def gift_landing_page(
    *,
    token: str,
    gift_summary: str,
    error: str = "",
    ok_message: str = "",
) -> str:
    err_html = f'<p class="err">{html.escape(error)}</p>' if error else ""
    ok_html = f'<p class="ok">{html.escape(ok_message)}</p>' if ok_message else ""
    form_html = (
        ""
        if ok_message
        else (
            f'<form method="post" action="/gift/{html.escape(token)}">'
            '<label for="email">登录邮箱</label>'
            '<input id="email" name="email" type="email" required autocomplete="email" placeholder="you@pku.edu.cn">'
            '<label for="password">密码（至少 8 位）</label>'
            '<input id="password" name="password" type="password" minlength="8" required autocomplete="current-password" placeholder="请输入至少 8 位密码">'
            '<button type="submit">登录并直接领取</button>'
            "</form>"
        )
    )
    copy_footer = "" if ok_message else (
        '<aside class="gift-desktop-hint"><span>已安装客户端？礼包码 </span>'
        f'<code id="gift-code">{html.escape(token)}</code> '
        '<button type="button" id="copy-gift-code" aria-label="复制礼包码">复制</button>'
        '<small id="gift-copy-status" role="status" aria-live="polite"></small></aside>'
        '<style>.gift-desktop-hint{margin-top:20px;padding-top:14px;border-top:1px solid #e3eae4;font-size:12px;color:#73827b}'
        '#gift-code{display:inline-block;padding:5px 10px;background:#eef4ef;border-radius:8px;font-weight:700;overflow-wrap:anywhere}'
        '#copy-gift-code{display:inline-block;width:auto;margin:0;padding:3px 7px;background:transparent;color:#52695d;font-size:12px;font-weight:500}'
        '#gift-copy-status{display:block;margin-top:4px}@media(max-width:540px){main{margin:16px 12px;padding:24px 18px}}</style>'
        '<script>document.getElementById("copy-gift-code").addEventListener("click",async function(){'
        'var code=document.getElementById("gift-code").textContent;'
        'function fallback(){var input=document.createElement("textarea");input.value=code;document.body.appendChild(input);input.select();'
        'try{if(!document.execCommand("copy"))throw new Error("copy")}finally{input.remove()}}'
        'try{if(navigator.clipboard&&navigator.clipboard.writeText){try{await navigator.clipboard.writeText(code)}catch(e){fallback()}}else{fallback()}'
        'this.textContent="已复制";document.getElementById("gift-copy-status").textContent="礼包码已复制"}'
        'catch(e){document.getElementById("gift-copy-status").textContent="复制失败，请长按礼包码复制"}});</script>'
    )
    return _page(
        "领取专属权益礼包",
        "<h1>PKU All in One 专属礼包</h1>"
        f"<p>礼包内容：<strong>{html.escape(gift_summary)}</strong></p>"
        f"{err_html}{ok_html}{form_html}{copy_footer}"
        + (DOWNLOAD_CTA if ok_message else ""),
    )
