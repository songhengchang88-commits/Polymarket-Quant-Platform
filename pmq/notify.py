"""多通道统一免费消息通知引擎 (Notifier)
==============================================
支持 100% 完全免费的微信与 QQ 实时消息通道：
1. PushPlus (微信公众号卡片推送, 个人免费 200条/天)
2. WxPusher (微信开放推送, 个人免费 1000条/天)
3. 企业微信群机器人 Webhook (官方免费, 微信/企微无限量接收)
4. Qmsg 酱 (QQ 机器人消息推送, 免费版)
5. QQ 邮箱 SMTP (0第三方依赖, 手机微信+手机QQ同时双弹窗)
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from typing import Any, Dict, Optional
import requests

logger = logging.getLogger("pmq_notifier")


class Notifier:
    def __init__(self):
        # 1. 微信 PushPlus
        self.pushplus_token = os.getenv("PUSHPLUS_TOKEN", "").strip()

        # 2. 微信 WxPusher
        self.wxpusher_token = os.getenv("WXPUSHER_APP_TOKEN", "").strip()
        self.wxpusher_uid = os.getenv("WXPUSHER_UID", "").strip()

        # 3. 企业微信群机器人 Webhook (个人微信可接收)
        self.wechat_webhook = os.getenv("WECHAT_WEBHOOK_URL", "").strip()

        # 4. QQ Qmsg 酱
        self.qmsg_key = os.getenv("QMSG_KEY", "").strip()
        self.qmsg_qq = os.getenv("QMSG_QQ", "").strip()

        # 5. QQ 邮箱 SMTP (微信QQ双弹窗)
        self.smtp_host = os.getenv("SMTP_HOST", "smtp.qq.com").strip()
        self.smtp_port = int(os.getenv("SMTP_PORT", "465"))
        self.smtp_user = os.getenv("SMTP_USER", "").strip()
        self.smtp_pass = os.getenv("SMTP_PASS", "").strip()
        self.smtp_to = os.getenv("SMTP_TO", self.smtp_user).strip()

        # 6. Telegram 官方机器人通道
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.tg_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    @property
    def has_any_channel(self) -> bool:
        """是否配置了至少一个免费通知通道"""
        return bool(
            self.pushplus_token
            or (self.wxpusher_token and self.wxpusher_uid)
            or self.wechat_webhook
            or self.qmsg_key
            or (self.smtp_user and self.smtp_pass)
            or (self.tg_token and self.tg_chat_id)
        )

    def send(self, title: str, content: str, level: str = "INFO") -> Dict[str, Any]:
        """向所有已配置的免费通道广播推送消息"""
        results = {}

        # 1. PushPlus (微信)
        if self.pushplus_token:
            results["pushplus"] = self._send_pushplus(title, content)

        # 2. WxPusher (微信)
        if self.wxpusher_token and self.wxpusher_uid:
            results["wxpusher"] = self._send_wxpusher(title, content)

        # 3. 企业微信 Webhook
        if self.wechat_webhook:
            results["wechat_webhook"] = self._send_wechat_webhook(title, content)

        # 4. QQ Qmsg 酱
        if self.qmsg_key:
            results["qmsg"] = self._send_qmsg(title, content)

        # 5. QQ 邮箱 SMTP (手机微信+手机QQ双弹窗)
        if self.smtp_user and self.smtp_pass:
            results["smtp_email"] = self._send_email(title, content)

        # 6. Telegram 官方机器人通道
        if self.tg_token and self.tg_chat_id:
            results["telegram"] = self._send_telegram(title, content)

        if not results:
            logger.debug(f"[通知未配置通道] {title}: {content[:60]}")
            return {"ok": False, "reason": "未配置任何免费通知通道"}

        return {"ok": True, "results": results}

    def _send_pushplus(self, title: str, content: str) -> bool:
        """PushPlus 微信卡片推送 (100% 免费)"""
        try:
            url = "http://www.pushplus.plus/send"
            payload = {
                "token": self.pushplus_token,
                "title": f"【Polymarket】{title}",
                "content": content.replace("\n", "<br>"),
                "template": "html"
            }
            res = requests.post(url, json=payload, timeout=6)
            data = res.json()
            if data.get("code") != 200:
                logger.warning(f"PushPlus 返回异常: {data.get('msg') or data.get('data')}")
                return False
            return True
        except Exception as e:
            logger.warning(f"PushPlus 推送异常: {e}")
            return False

    def _send_wxpusher(self, title: str, content: str) -> bool:
        """WxPusher 微信消息推送 (100% 免费)"""
        try:
            url = "https://wxpusher.zjiecode.com/api/send/message"
            payload = {
                "appToken": self.wxpusher_token,
                "content": f"## {title}\n\n{content}",
                "summary": title[:20],
                "contentType": 3,  # Markdown 格式
                "uids": [self.wxpusher_uid],
            }
            res = requests.post(url, json=payload, timeout=6)
            return res.json().get("code") == 1000
        except Exception as e:
            logger.warning(f"WxPusher 推送异常: {e}")
            return False

    def _send_wechat_webhook(self, title: str, content: str) -> bool:
        """企业微信群机器人 Webhook (100% 官方免费)"""
        try:
            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "content": f"### <font color=\"info\">【Polymarket 哨兵】{title}</font>\n\n{content}"
                }
            }
            res = requests.post(self.wechat_webhook, json=payload, timeout=6)
            return res.json().get("errcode") == 0
        except Exception as e:
            logger.warning(f"企业微信 Webhook 推送异常: {e}")
            return False

    def _send_qmsg(self, title: str, content: str) -> bool:
        """QQ Qmsg 酱推送 (100% 免费)"""
        try:
            url = f"https://qmsg.zendee.cn/send/{self.qmsg_key}"
            msg_text = f"【Polymarket】{title}\n----------------\n{content}"
            payload = {"msg": msg_text}
            if self.qmsg_qq:
                payload["qq"] = self.qmsg_qq
            res = requests.post(url, data=payload, timeout=6)
            data = res.json()
            if not (data.get("success", False) or data.get("code") == 0):
                logger.warning(f"Qmsg 提示: {data.get('message') or data.get('reason')}")
                return False
            return True
        except Exception as e:
            logger.warning(f"Qmsg 推送异常: {e}")
            return False

    def _send_email(self, title: str, content: str) -> bool:
        """QQ 邮箱 SMTP (手机 QQ + 微信同时弹窗通知)"""
        try:
            msg = MIMEText(content, "plain", "utf-8")
            msg["Subject"] = Header(f"【Polymarket】{title}", "utf-8")
            msg["From"] = self.smtp_user
            msg["To"] = self.smtp_to

            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=8) as server:
                server.login(self.smtp_user, self.smtp_pass)
                server.sendmail(self.smtp_user, [self.smtp_to], msg.as_string())
            return True
        except Exception as e:
            logger.warning(f"邮件 SMTP 推送异常: {e}")
            return False

    def _send_telegram(self, title: str, content: str) -> bool:
        """Telegram 官方机器人推送"""
        try:
            url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
            text = f"🛡️ *【Polymarket 哨兵】{title}*\n\n{content}"
            payload = {
                "chat_id": self.tg_chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True
            }
            res = requests.post(url, json=payload, timeout=8)
            return res.json().get("ok", False)
        except Exception as e:
            logger.warning(f"Telegram 推送异常: {e}")
            return False


# 全局单例
notifier = Notifier()
