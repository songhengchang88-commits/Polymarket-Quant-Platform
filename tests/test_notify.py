from unittest.mock import patch, MagicMock
from pmq.notify import Notifier


def test_notifier_empty_config():
    n = Notifier()
    # 默认未配任何 key
    res = n.send("测试标题", "测试内容")
    assert res["ok"] is False


@patch("requests.post")
def test_notifier_pushplus_mock(mock_post):
    mock_post.return_value.json.return_value = {"code": 200}
    n = Notifier()
    n.pushplus_token = "mock_token"
    res = n.send("实盘报警", "死人开关触发")
    assert res["ok"] is True
    assert res["results"]["pushplus"] is True
    mock_post.assert_called_once()


@patch("requests.post")
def test_notifier_wechat_webhook_mock(mock_post):
    mock_post.return_value.json.return_value = {"errcode": 0}
    n = Notifier()
    n.wechat_webhook = "https://qyapi.weixin.qq.com/mock"
    res = n.send("巡航播报", "6.98U 资金安全")
    assert res["ok"] is True
    assert res["results"]["wechat_webhook"] is True


@patch("requests.post")
def test_notifier_qmsg_mock(mock_post):
    mock_post.return_value.json.return_value = {"code": 0}
    n = Notifier()
    n.qmsg_key = "mock_qmsg_key"
    res = n.send("QQ测试", "收到请回复")
    assert res["ok"] is True
    assert res["results"]["qmsg"] is True
