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
EXTRA_CIDRS = [x.strip() for x in os.getenv("EXTRA_CIDRS", "").split(",") if x.strip()]
EXTRA_FALLBACK = [x.strip().lstrip(".").lower() for x in os.getenv("EXTRA_FALLBACK", "").split(",") if x.strip()]
DRY_RUN = os.getenv("DRY_RUN", "").lower() == "true"

BASE = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}"

CIDR_URL = "https://raw.githubusercontent.com/soffchen/GeoIP2-CN/release/CN-ip-cidr.csv"
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


def cf_api(method, path, payload=None):
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
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        die(f"Cloudflare API {method} {path} 失败 HTTP {e.code}:\n{body}")


def load_cn_cidrs():
    print("🔄 拉取 CN CIDR 数据...")
    text = fetch(CIDR_URL)
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

    # 域名预算：精选优先，再从大列表补足
    fallback = list(DEFAULT_FALLBACK)
    for d in CURATED_CN_DOMAINS + domains:
        if len(fallback) >= DOMAIN_LIMIT:
            break
        if d not in fallback:
            fallback.append(d)
    for d in EXTRA_FALLBACK:
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
    for c in cidrs:
        if len(exclude) >= IP_LIMIT + len(DEFAULT_EXCLUDE) + len(EXTRA_CIDRS):
            break
        exclude.append({"address": c, "description": "CN"})

    print(f"📊 统计：fallback 域名 {len(fallback)} 条（精选 {min(len(CURATED_CN_DOMAINS), DOMAIN_LIMIT)} 条优先）")
    print(f"📊 统计：exclude IP {len(exclude)} 条（默认 {len(DEFAULT_EXCLUDE)} + 自定义 {len(EXTRA_CIDRS)} + CN {len(exclude) - len(DEFAULT_EXCLUDE) - len(EXTRA_CIDRS)}）")

    if DRY_RUN:
        print("🏁 DRY_RUN 模式，不写云端。确认无误后去掉 DRY_RUN 重跑。")
        return

    print("🔄 读取当前设备策略...")
    resp = cf_api("GET", "/devices/policy")
    policy = resp.get("result")
    if isinstance(policy, list):
        die("API 返回了旧版列表结构，v2 只适配 2026 单对象模型，请人工确认")
    if not isinstance(policy, dict) or "policy_id" not in policy:
        die(f"无法解析设备策略对象：{json.dumps(policy)[:200]}")

    policy["exclude"] = exclude
    policy["fallback_domains"] = [{"suffix": d} for d in fallback]

    print("🔄 整体写回设备策略...")
    resp = cf_api("PUT", "/devices/policy", payload=policy)
    if resp.get("success"):
        print(f"✅ 成功：fallback {len(fallback)} 条，exclude {len(exclude)} 条")
        print("⏳ 策略下发到客户端最长需要 10 分钟")
    else:
        die(f"写入失败：{json.dumps(resp.get('errors'), ensure_ascii=False)}")


if __name__ == "__main__":
    main()
