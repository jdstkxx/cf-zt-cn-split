#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cf-zt-cn-split v2 —— 适配 Cloudflare 2026 单配置文件模型的改写版

改进点（相对原 cf-zt-cn-split）：
1. 适配新 API：整体读写 /accounts/{id}/devices/policy，不再需要 CF_PROFILE_ID（留空即可，或不配置）
2. 域名不塞进 Split Tunnels（官方：移动端域名分流仅在隧道启动时生效；example.com 不自动含子域名），
   改为写入 Local Domain Fallback：国内域名用回本地 DNS 解析，根治 CDN 调度跑偏（淘宝/微信图片慢的根因）
3. 保留默认私网排除与默认 fallback 后缀；支持 EXTRA_CIDRS / EXTRA_FALLBACK 追加自定义条目
4. DRY_RUN 模式：只统计不写，先跑一遍确认再正式写入
5. 写入失败时打印 Cloudflare 返回的完整错误体，方便排查

环境变量：
  CF_API_TOKEN    必填（帐户 / Zero Trust / 编辑）
  CF_ACCOUNT_ID   必填
  DOMAIN_LIMIT    fallback 域名预算，默认 250
  IP_LIMIT        CN CIDR 预算，默认 3650
  EXTRA_CIDRS     逗号分隔的自定义 CIDR，如 "192.168.188.0/24,10.99.0.0/16"
  EXTRA_FALLBACK  逗号分隔的自定义 fallback 后缀，如 "corp.example.com"
  DRY_RUN         设成 true 时只打印不写入
"""

import json
import os
import sys
import ipaddress
import urllib.request

CF_API_TOKEN = os.getenv("CF_API_TOKEN", "").strip()
ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "").strip()
DOMAIN_LIMIT = int(os.getenv("DOMAIN_LIMIT", "250"))
IP_LIMIT = int(os.getenv("IP_LIMIT", "3650"))
import re as _re

def _split_env(s):
    # 支持逗号 / 中文逗号 / 空格 / 分号 / 换行混用分隔
    return [x.strip() for x in _re.split(r"[,，;；\s]+", s) if x.strip()]

EXTRA_CIDRS = _split_env(os.getenv("EXTRA_CIDRS", ""))
EXTRA_FALLBACK = [x.lstrip(".").lower() for x in _split_env(os.getenv("EXTRA_FALLBACK", ""))]
DRY_RUN = os.getenv("DRY_RUN", "").lower() == "true"

BASE = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}"

# 多源兜底：GeoIP2-CN 仓库已失效，改用 clang.cn 为主、17mon 为备
CIDR_URLS = [
    "https://ispip.clang.cn/all_cn.txt",
    "https://raw.githubusercontent.com/17mon/china_ip_list/master/china_ip_list.txt",
]
DOMAIN_URL = "https://raw.githubusercontent.com/Loyalsoldier/surge-rules/release/direct.txt"

# Cloudflare 默认的 15 条私网排除（重建 exclude 时保留，保证局域网/组播正常）
DEFAULT_EXCLUDE = [
    "10.0.0.0/8", "100.64.0.0/10", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.168.0.0/16", "224.0.0.0/24", "240.0.0.0/4",
    "255.255.255.255/32",
    "fe80::/10", "fd00::/8", "ff01::/16", "ff02::/16", "ff03::/16",
    "ff04::/16", "ff05::/16",
]

# Cloudflare 默认的 13 个 fallback 后缀（重建时保留）
DEFAULT_FALLBACK = [
    "home.arpa", "intranet", "internal", "private", "localdomain", "domain",
    "lan", "home", "host", "corp", "local", "localhost", "invalid", "test",
]

# 精选：CDN 调度敏感、用户高频的国内域名（放最前面，优先占额度）
# 国内三大运营商 IPv6 大段（GeoIP2-CN 数据源只有 v4，v6 内置维护）
CN_V6 = [
    "240e::/16",   # 中国电信
    "2408::/16",   # 中国联通
    "2409::/16",   # 中国移动
]

CURATED_CN_DOMAINS = [
    "taobao.com", "alicdn.com", "tbcdn.cn", "tmall.com", "alipay.com",
    "alipayobjects.com", "qq.com", "qpic.cn", "gtimg.cn", "qcloud.com",
    "myqcloud.com", "wechat.com", "servicewechat.com", "tencent.com",
    "tencentyun.com", "baidu.com", "bdimg.com", "bdstatic.com", "bcebos.com",
    "bilibili.com", "biliapi.net", "hdslb.com", "jd.com", "jdcdn.com",
    "360buyimg.com", "meituan.com", "dianping.com", "sinaimg.cn", "weibo.com",
    "douyin.com", "bytecdn.cn", "bytedance.com", "toutiao.com", "pddcdn.com",
    "yangkeduo.com", "aliyuncs.com", "aliyun.com", "xiaohongshu.com",
    "zhimg.com", "csdnimg.cn", "163.com", "126.net", "netease.com",
    "126.com", "qqmail.com", "sogou.com", "sogoucdn.com", "sohu.com",
    "sohucdn.com", "youku.com", "ykimg.com", "iqiyi.com", "qiyi.com",
    "qiyipic.com", "kuaishou.com", "yximgs.com", "ctrip.com", "tuniu.com",
    "mafengwo.net", "dangdang.com", "suning.com", "gome.com.cn", "vancl.com",
    "mogujie.com", "smzdm.com", "zdmimg.com", "ithome.com", "ruanmei.com",
    "sspai.com", "juejin.cn", "csdn.net", "oschina.net", "gitee.com",
    "alibaba.com", "aliexpress.com", "dingtalk.com", "feishu.cn",
    "larksuite.com", "wps.cn", "wpscdn.com", "kingsoft.com", "qiniu.com",
    "qiniucdn.com", "upyun.com", "cdn20.com", "bootcdn.net", "staticfile.org",
]


def die(msg):
    print(f"❌ {msg}")
    sys.exit(1)


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "cf-zt-cn-split-v2"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def cf_api_raw(method, path, payload=None):
    """返回 (是否成功, 响应体文本)，不再自行退出"""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={
            "Authorization": f"Bearer {CF_API_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return True, r.read().decode()
    except urllib.error.HTTPError as e:
        return False, e.read().decode("utf-8", "ignore")


def load_cn_cidrs():
    print("🔄 拉取 CN CIDR 数据...")
    text, used = None, None
    for url in CIDR_URLS:
        try:
            text = fetch(url)
            used = url
            break
        except Exception as e:
            print(f"   ⚠️ 数据源不可用 {url}：{e}")
    if text is None:
        die("所有 CIDR 数据源均不可用，请检查网络或更新 CIDR_URLS")
    print(f"   使用数据源：{used}")
    out, seen = [], set()
    for line in text.splitlines():
        c = line.strip()
        if not c or "/" not in c:
            continue
        try:
            net = ipaddress.ip_network(c, strict=False)
        except ValueError:
            continue
        if net.version != 4:          # 只保留 IPv4，WARP 分流对 v6 场景有限
            continue
        key = str(net)
        if key not in seen:
            seen.add(key)
            out.append(key)
    print(f"   CIDR 数据源：{len(out)} 条（IPv4）")
    return out


def load_cn_domains():
    print("🔄 拉取 CN 域名数据...")
    text = fetch(DOMAIN_URL)
    out, seen = [], set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        # surge 规则格式：DOMAIN-SUFFIX,xxx.com / DOMAIN,xxx.com / 纯域名
        d = line.split(",")[-1].strip().lower().lstrip(".").rstrip(".")
        if not d or "." not in d or "*" in d or len(d) > 60:
            continue
        if d not in seen:
            seen.add(d)
            out.append(d)
    print(f"   域名数据源：{len(out)} 条")
    return out


def main():
    if not CF_API_TOKEN or not ACCOUNT_ID:
        die("缺少 CF_API_TOKEN 或 CF_ACCOUNT_ID 环境变量")

    cidrs = load_cn_cidrs()
    domains = load_cn_domains()

    # 域名预算：默认后缀 → 自定义（永远最前，不被限额挤掉）→ 精选 → 大列表补足
    fallback = list(DEFAULT_FALLBACK)
    for d in EXTRA_FALLBACK:
        if d not in fallback:
            fallback.append(d)
    for d in CURATED_CN_DOMAINS + domains:
        if len(fallback) >= DOMAIN_LIMIT + len(EXTRA_FALLBACK):
            break
        if d not in fallback:
            fallback.append(d)

    # IP 预算：默认私网 + 自定义 + CN CIDR
    exclude = [{"address": c} for c in DEFAULT_EXCLUDE]
    for c in EXTRA_CIDRS:
        try:
            ipaddress.ip_network(c, strict=False)
        except ValueError:
            die(f"EXTRA_CIDRS 里有非法 CIDR：{c}")
        exclude.append({"address": c, "description": "custom"})
    for c in CN_V6:
        exclude.append({"address": c, "description": "CN-IPv6"})
    for c in cidrs:
        if len(exclude) >= IP_LIMIT + len(DEFAULT_EXCLUDE) + len(EXTRA_CIDRS) + len(CN_V6):
            break
        exclude.append({"address": c, "description": "CN"})

    print(f"📊 统计：fallback 域名 {len(fallback)} 条（精选 {min(len(CURATED_CN_DOMAINS), DOMAIN_LIMIT)} 条优先）")
    print(f"📊 统计：exclude IP {len(exclude)} 条（默认 {len(DEFAULT_EXCLUDE)} + 自定义 {len(EXTRA_CIDRS)} + CN-v6 {len(CN_V6)} + CN-v4 {len(exclude) - len(DEFAULT_EXCLUDE) - len(EXTRA_CIDRS) - len(CN_V6)}）")

    if DRY_RUN:
        print("🏁 DRY_RUN 模式，不写云端。确认无误后去掉 DRY_RUN 重跑。")
        return

    print("🔄 读取当前设备策略...")
    ok, body = cf_api_raw("GET", "/devices/policy")
    if not ok:
        die(f"读取设备策略失败：{body[:300]}")
    policy = json.loads(body).get("result")
    if isinstance(policy, list):
        die("API 返回了旧版列表结构，v2 只适配 2026 单对象模型，请人工确认")
    if not isinstance(policy, dict) or "policy_id" not in policy:
        die(f"无法解析设备策略对象：{json.dumps(policy)[:200]}")

    policy["exclude"] = exclude
    policy["fallback_domains"] = [{"suffix": d} for d in fallback]

    print("🔄 写回 Split Tunnels 排除列表...")
    # 新模型实证端点（来自原版 cf-zt-cn-split 的成功调用）：
    #   PUT /accounts/{aid}/devices/policy/exclude
    #   body = 纯数组，IP 条目 {"address": cidr}，域名条目 {"host": domain}
    #   语义：整体替换——所以数组里必须自带默认私网段和自定义条目
    routes = exclude
    ok, body = cf_api_raw("PUT", "/devices/policy/exclude", routes)
    if not ok:
        die(f"Split Tunnels 写入失败：{body[:300]}")
    print(f"✅ Split Tunnels 写入成功：{len(routes)} 条")

    print("🔄 写回 Local Domain Fallback...")
    # fallback_domains 没有公开的新模型端点，逐级尝试；失败不阻塞主流程
    fb = [{"suffix": d} for d in fallback]
    fb_attempts = [
        ("PATCH", "/devices/policy", {"fallback_domains": fb}),
        ("PUT", "/devices/policy/fallback_domains", fb),
    ]
    fb_ok = False
    fb_err = ""
    for method, path, payload in fb_attempts:
        ok, body = cf_api_raw(method, path, payload)
        if ok:
            print(f"✅ Local Domain Fallback 写入成功（{method} {path}）：{len(fb)} 条")
            fb_ok = True
            break
        fb_err = f"{method} {path} -> {body[:200]}"
        print(f"   ⚠️ {method} {path} 不通：{body[:150]}")
    if not fb_ok:
        print("⚠️ Local Domain Fallback 写入失败（不影响 Split Tunnels，请把此信息反馈）：")
        print(f"   {fb_err}")

    print("⏳ 策略下发到客户端最长需要 10 分钟")

if __name__ == "__main__":
    main()
