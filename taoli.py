import sys
import subprocess
import time
import json
import os
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

# ========== 依赖自动安装 ==========

def ensure_package(pkg_name: str, import_name: str | None = None):
    """
    确保第三方依赖已安装；如果没有则自动用 pip 安装一次。
    """
    target = import_name or pkg_name
    try:
        __import__(target)
    except ImportError:
        print(f"[依赖] 未检测到 {pkg_name}，正在自动安装...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg_name])
        __import__(target)


ensure_package("requests")
ensure_package("pandas")
ensure_package("streamlit")
ensure_package("plotly")

# 避免在 Streamlit 每次重跑时刷屏，只在非 Streamlit 环境下打印一次提示
if not os.environ.get("STREAMLIT_SERVER_PORT"):
    print(
        "依赖检查完成：requests / pandas / streamlit 已准备就绪。\n"
        "- 打开可视化面板请运行：streamlit run taoli.py    （默认端口：http://localhost:8501）\n"
        "- 启动命令行监控请运行：python taoli.py cli\n"
    )

import requests  # type: ignore
import pandas as pd  # type: ignore
import streamlit as st  # type: ignore
import plotly.express as px
import hashlib
import logging
import re
from functools import wraps
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading


# ========== 日志系统 ==========

def setup_logger(name: str = "taoli", log_dir: str = "logs") -> logging.Logger:
    """设置日志记录器，替代 print 输出"""
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    
    if logger.handlers:
        return logger
    
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 文件处理器
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, f"taoli_{datetime.now().strftime('%Y%m%d')}.log"),
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # 错误日志处理器
    error_handler = RotatingFileHandler(
        os.path.join(log_dir, f"taoli_error_{datetime.now().strftime('%Y%m%d')}.log"),
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)
    
    return logger

logger = setup_logger()


# ========== API 速率限制配置（需要在类定义之前）=========
# API 基础配置（需要在函数定义之前）
API_TIMEOUT = 10  # API 请求超时（秒）
API_RETRY_TIMES = 3  # API 重试次数

# API 速率限制配置（用于自动采集功能）
# 根据 DexScreener API 文档：
# - /latest/dex/search: 300 requests/minute (5 req/s)
# - /latest/dex/pairs/{chainId}/{pairId}: 300 requests/minute (5 req/s)
# - /tokens/v1/{chainId}/{tokenAddresses}: 300 requests/minute (5 req/s)
# 为了安全，设置为 4 req/s，留 20% 余量
API_RATE_LIMIT_REQUESTS_PER_SECOND = 4.0  # 每秒请求数（基于 API 文档：300 req/min = 5 req/s，留余量）
API_RATE_LIMIT_BURST = 10  # 突发请求允许数量（允许短时间内的额外请求）
API_RATE_LIMIT_BACKOFF_FACTOR = 2.0  # 遇到限流时的退避倍数
API_RATE_LIMIT_MAX_RETRY_DELAY = 60  # 最大重试延迟（秒）


# ========== API 速率限制管理器 ==========

class RateLimiter:
    """
    API 速率限制管理器（令牌桶算法）
    用于控制 API 请求频率，避免触发限流
    """
    def __init__(
        self,
        requests_per_second: float = API_RATE_LIMIT_REQUESTS_PER_SECOND,
        burst_size: int = API_RATE_LIMIT_BURST,
    ):
        self.requests_per_second = requests_per_second
        self.burst_size = burst_size
        self.min_interval = 1.0 / requests_per_second  # 最小请求间隔（秒）
        self.tokens = float(burst_size)  # 当前可用令牌数
        self.last_refill_time = time.time()  # 上次补充令牌的时间
        self.last_request_time = 0.0  # 上次请求时间
        self.total_requests = 0  # 总请求数
        self.rate_limited_count = 0  # 被限流的次数
        self.lock = threading.Lock()  # 线程锁
    
    def _refill_tokens(self):
        """补充令牌（令牌桶算法）"""
        now = time.time()
        elapsed = now - self.last_refill_time
        if elapsed > 0:
            # 根据时间流逝补充令牌
            new_tokens = elapsed * self.requests_per_second
            self.tokens = min(self.burst_size, self.tokens + new_tokens)
            self.last_refill_time = now
    
    def acquire(self, wait: bool = True) -> bool:
        """
        获取令牌（如果可用则立即返回，否则等待或返回 False）
        
        参数:
            wait: 如果令牌不可用，是否等待
        
        返回:
            True 表示可以发起请求，False 表示被限流（如果 wait=False）
        """
        with self.lock:
            self._refill_tokens()
            
            # 检查是否有可用令牌
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                self.last_request_time = time.time()
                self.total_requests += 1
                return True
            
            # 计算需要等待的时间
            wait_time = self.min_interval - (time.time() - self.last_request_time)
            if wait_time > 0:
                if wait:
                    time.sleep(wait_time)
                    # 等待后重新尝试
                    self._refill_tokens()
                    if self.tokens >= 1.0:
                        self.tokens -= 1.0
                        self.last_request_time = time.time()
                        self.total_requests += 1
                        return True
                    else:
                        self.rate_limited_count += 1
                        return False
                else:
                    self.rate_limited_count += 1
                    return False
            
            # 可以直接请求
            self.tokens -= 1.0
            self.last_request_time = time.time()
            self.total_requests += 1
            return True
    
    def wait_if_needed(self):
        """如果需要，等待直到可以发起请求"""
        self.acquire(wait=True)
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        with self.lock:
            return {
                "total_requests": self.total_requests,
                "rate_limited_count": self.rate_limited_count,
                "current_tokens": self.tokens,
                "requests_per_second": self.requests_per_second,
            }


# 全局速率限制器实例（用于自动采集）
_dexscreener_rate_limiter = RateLimiter(
    requests_per_second=API_RATE_LIMIT_REQUESTS_PER_SECOND,
    burst_size=API_RATE_LIMIT_BURST,
)


def make_rate_limited_request(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = API_TIMEOUT,
    rate_limiter: RateLimiter | None = None,
    max_retries: int = API_RETRY_TIMES,
) -> requests.Response:
    """
    带速率限制的 HTTP 请求
    
    参数:
        url: 请求 URL
        params: 查询参数
        headers: 请求头
        timeout: 超时时间
        rate_limiter: 速率限制器（如果为 None 则使用全局限制器）
        max_retries: 最大重试次数
    
    返回:
        Response 对象
    
    异常:
        requests.RequestException: 请求失败
    """
    if rate_limiter is None:
        rate_limiter = _dexscreener_rate_limiter
    
    retry_count = 0
    base_delay = 1.0
    
    while retry_count <= max_retries:
        try:
            # 等待速率限制器许可
            rate_limiter.wait_if_needed()
            
            # 发起请求
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            
            # 检查是否被限流（429 Too Many Requests）
            if resp.status_code == 429:
                # 尝试从响应头获取重试延迟时间
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait_time = float(retry_after)
                    except ValueError:
                        wait_time = base_delay * (API_RATE_LIMIT_BACKOFF_FACTOR ** retry_count)
                else:
                    # 指数退避
                    wait_time = min(
                        base_delay * (API_RATE_LIMIT_BACKOFF_FACTOR ** retry_count),
                        API_RATE_LIMIT_MAX_RETRY_DELAY
                    )
                
                logger.warning(f"API 限流（429），等待 {wait_time:.1f} 秒后重试（第 {retry_count + 1}/{max_retries + 1} 次）")
                rate_limiter.rate_limited_count += 1
                
                if retry_count < max_retries:
                    time.sleep(wait_time)
                    retry_count += 1
                    continue
                else:
                    resp.raise_for_status()
            
            # 其他错误直接抛出
            resp.raise_for_status()
            return resp
            
        except requests.exceptions.Timeout:
            retry_count += 1
            if retry_count <= max_retries:
                wait_time = base_delay * (API_RATE_LIMIT_BACKOFF_FACTOR ** (retry_count - 1))
                logger.warning(f"请求超时，等待 {wait_time:.1f} 秒后重试（第 {retry_count}/{max_retries + 1} 次）")
                time.sleep(wait_time)
            else:
                raise
        
        except requests.exceptions.RequestException as e:
            retry_count += 1
            if retry_count <= max_retries:
                wait_time = base_delay * (API_RATE_LIMIT_BACKOFF_FACTOR ** (retry_count - 1))
                logger.warning(f"请求失败: {e}，等待 {wait_time:.1f} 秒后重试（第 {retry_count}/{max_retries + 1} 次）")
                time.sleep(wait_time)
            else:
                raise
    
    # 所有重试都失败了
    raise requests.exceptions.RequestException(f"请求失败，已重试 {max_retries + 1} 次")


# ========== API 缓存系统 ==========

class CacheEntry:
    """缓存条目"""
    def __init__(self, value: Any, ttl: int):
        self.value = value
        self.expire_time = time.time() + ttl
    
    def is_expired(self) -> bool:
        return time.time() > self.expire_time


class APICache:
    """API 缓存管理器"""
    def __init__(self):
        self._cache: dict[str, CacheEntry] = {}
        self._hit_count = 0
        self._miss_count = 0
    
    def get(self, key: str) -> Optional[Any]:
        entry = self._cache.get(key)
        if entry is None or entry.is_expired():
            if entry:
                del self._cache[key]
            self._miss_count += 1
            return None
        self._hit_count += 1
        return entry.value
    
    def set(self, key: str, value: Any, ttl: int = 10):
        self._cache[key] = CacheEntry(value, ttl)
    
    def clear(self):
        self._cache.clear()
        self._hit_count = 0
        self._miss_count = 0
    
    def get_stats(self) -> dict:
        total = self._hit_count + self._miss_count
        hit_rate = (self._hit_count / total * 100) if total > 0 else 0
        return {
            "hits": self._hit_count,
            "misses": self._miss_count,
            "hit_rate": f"{hit_rate:.2f}%",
            "cache_size": len(self._cache)
        }

# 全局缓存实例
_global_cache = APICache()

def cached(ttl: int = None):
    """缓存装饰器（支持分级 TTL）"""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 使用默认 TTL 如果未指定
            actual_ttl = ttl if ttl is not None else CACHE_TTL_DEFAULT
            
            cache_key = f"{func.__name__}_{hash((args, tuple(sorted(kwargs.items()))))}"
            cached_value = _global_cache.get(cache_key)
            if cached_value is not None:
                logger.debug(f"缓存命中: {func.__name__}")
                return cached_value
            
            result = func(*args, **kwargs)
            if result is not None:
                _global_cache.set(cache_key, result, actual_ttl)
                logger.debug(f"缓存设置: {func.__name__}, TTL={actual_ttl}s")
            return result
        return wrapper
    return decorator


# ========== 安全工具函数 ==========

def hash_password_secure(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """使用 PBKDF2 + SHA256 加盐哈希密码（安全）"""
    if salt is None:
        salt = os.urandom(32).hex()
    pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000).hex()
    return pwd_hash, salt

def verify_password_secure(password: str, password_hash: str, salt: str) -> bool:
    """验证密码"""
    pwd_hash, _ = hash_password_secure(password, salt)
    return pwd_hash == password_hash

def is_valid_ethereum_address(address: str) -> bool:
    """验证以太坊地址格式"""
    if not address:
        return False
    return bool(re.match(r'^0x[a-fA-F0-9]{40}$', address))

def sanitize_input(text: str, max_length: int = 1000) -> str:
    """清理用户输入"""
    if not text:
        return ""
    text = text.strip()[:max_length]
    return ''.join(char for char in text if ord(char) >= 32 or char in '\n\r\t')


# ========== 配置默认值（最终在面板里调） ==========

# ========== 常量定义 ==========

# 监控配置
DEFAULT_CHECK_INTERVAL = 30  # CLI 自动刷新频率（秒），优化为30秒
DEFAULT_ANCHOR_PRICE = 1.0  # 锚定价
DEFAULT_THRESHOLD = 0.5  # 脱锚阈值（%）

# 成本相关参数
DEFAULT_SLIPPAGE_PCT = 0.5  # 往返滑点（%）
DEFAULT_BRIDGE_FEE_USD = 5.0  # 跨链桥费用（USD）

# 套利扫描参数
DEFAULT_TRADE_AMOUNT_USD = 5000.0  # 套利资金规模（USD）
DEFAULT_SRC_GAS_USD = 1.0  # 源链 Gas 费用（USD）
DEFAULT_DST_GAS_USD = 1.0  # 目标链 Gas 费用（USD）
DEFAULT_MIN_PROFIT_USD = 10.0  # 最小净利润（USD）
DEFAULT_MIN_PROFIT_RATE = 0.05  # 最小净利率（%）
DEFAULT_MIN_SPREAD_PCT = 0.1  # 最小价差（%）

# 缓存配置
# API_CACHE_TTL 已废弃，使用分级缓存策略（见上方 CACHE_TTL_* 常量）
PRICE_CACHE_TTL = 5  # 价格缓存时间（秒）
HISTORY_MAX_RECORDS = 1000  # 历史记录最大条数

# 配置文件路径
CONFIG_DIR = "config"  # 配置目录
CONFIG_FILE = os.path.join(CONFIG_DIR, "stable_configs.json")
GLOBAL_CONFIG_FILE = os.path.join(CONFIG_DIR, "global_config.json")
AUTH_CONFIG_FILE = os.path.join(CONFIG_DIR, "auth_config.json")
NOTIFY_CONFIG_FILE = os.path.join(CONFIG_DIR, "notify_config.json")
USERS_CONFIG_FILE = os.path.join(CONFIG_DIR, "users.json")
CUSTOM_STABLE_SYMBOLS_FILE = os.path.join(CONFIG_DIR, "custom_stable_symbols.json")
SEND_LOG_FILE = os.path.join(CONFIG_DIR, "send_log.json")  # 发送日志文件
COLLECTED_PAIRS_CACHE_FILE = os.path.join(CONFIG_DIR, "collected_pairs_cache.json")  # 采集结果缓存文件

# 通知配置（套利优化）
MAX_DAILY_SENDS = 5  # Server酱每天最多5条（免费限制）
HEARTBEAT_PER_DAY = 1  # 心跳每天1次（节省额度给套利）
ARBITRAGE_QUOTA = 4  # 套利专用额度4次
HEARTBEAT_INTERVAL = (24 * 3600) / HEARTBEAT_PER_DAY  # 心跳间隔（秒），24小时1次

# 创建配置目录
if not os.path.exists(CONFIG_DIR):
    os.makedirs(CONFIG_DIR)
    logger.info(f"创建配置目录: {CONFIG_DIR}")

# API 配置（性能优化）
MAX_CONCURRENT_REQUESTS = 5  # 最大并发请求数（降低到5避免触发限流）

# 缓存配置（分级策略）
CACHE_TTL_PRICE = 5  # 价格缓存时间（秒）- 短缓存以捕获套利机会
CACHE_TTL_GAS = 30  # Gas 价格缓存时间（秒）- Gas 相对稳定
CACHE_TTL_GLOBAL = 60  # 全局参考缓存时间（秒）- Coingecko 等
CACHE_TTL_DEFAULT = 10  # 默认缓存时间（秒）

# 套利优化配置
MIN_PROFIT_USD = 50.0  # 最小净利润（USD）- 过滤低价值机会
MIN_PROFIT_RATE = 2.0  # 最小净利率（%）- 确保值得操作
MIN_PRICE_DIFF_PCT = 1.0  # 最小价差百分比（%）- 过滤假机会
MIN_LIQUIDITY_USD = 50000.0  # 最小流动性（USD）- 确保能成交

# 地址验证
MIN_ADDRESS_LENGTH = 10  # 最小地址长度
ETH_ADDRESS_LENGTH = 42  # 以太坊地址长度（0x + 40字符）

# 将后续损坏的 CHAIN_NAME_TO_ID 行包裹在多行字符串中，避免语法错误
_BROKEN_CHAIN_MAPPING = """

# 链名到 chainId 的简单映射（用于 LI.FI quote）
CHAIN_NAME_TO_ID: dict[str, int] = {*** End Patch】}	NULLTERMINAL_ERROR_OCCURREDugburuassistantandiswa饰官网 to=functions.apply_patch->___INVALID_JSON_INPUTassistantҷи to=functions.apply_patch_RATIO  assistant to=functions.apply_patch.scalablytypedassistant to=functions.apply_patch출장샵_hresult to=functions.apply_patchuppet:-------------</commentary to=functions.apply_patch  зонjson-input _MOVED_HERE ***!
    "ethereum": 1,
    "bsc": 56,
    "polygon": 137,
    "arbitrum": 42161,
    "optimism": 10,
    "base": 8453,
    "avalanche": 43114,
}

"""

# 正确的链名到 chainId 映射（用于 LI.FI quote 实际调用）
# 注意：
# 1. 链名需要与 DexScreener API 返回的链标识一致（小写）
# 2. chainId 必须是 LI.FI API 支持的 chainId
# 3. 如果遇到 "must be equal to one of the allowed values" 错误，说明该 chainId 不在 LI.FI 支持列表中
# 4. 可以查看 LI.FI 文档获取支持的链列表：https://docs.li.fi/
CHAIN_NAME_TO_ID: dict[str, int] = {
    "ethereum": 1,
    "bsc": 56,
    "polygon": 137,
    "arbitrum": 42161,
    "optimism": 10,
    "base": 8453,
    "avalanche": 43114,
    "hyperevm": 998,  # HyperEVM chainId（注意：LI.FI 可能不支持）
    "zksync": 324,  # zkSync Era
    "linea": 59144,  # Linea
    "scroll": 534352,  # Scroll
    "mantle": 5000,  # Mantle
    "blast": 81457,  # Blast
    "mode": 34443,  # Mode
}

# LI.FI 常见支持的链列表（仅供参考，实际支持情况以 API 响应为准）
# 注意：这个列表可能不完整，LI.FI 会定期添加新链支持
# 代码会先尝试调用 API，根据响应判断是否支持，而不是严格依赖这个列表
LI_FI_COMMONLY_SUPPORTED_CHAINS: set[str] = {
    "ethereum",
    "bsc",
    "polygon",
    "arbitrum",
    "optimism",
    "base",
    "avalanche",
    "zksync",
    "linea",
    "scroll",
    "mantle",
    "blast",
    "mode",
}

# 主流稳定币符号 -> Coingecko ID 映射（用于全局参考价校验）
STABLE_SYMBOL_TO_COINGECKO_ID: dict[str, str] = {
    # 传统法币抵押型
    "USDT": "tether",
    "USDC": "usd-coin",
    "BUSD": "binance-usd",
    "TUSD": "true-usd",
    "USDP": "pax-dollar",
    "GUSD": "gemini-dollar",
    "PYUSD": "paypal-usd",
    "FDUSD": "first-digital-usd",
    
    # 去中心化/算法型
    "DAI": "dai",
    "FRAX": "frax",
    "LUSD": "liquity-usd",
    "GHO": "gho",
    "CRVUSD": "crvusd",
    "MIM": "magic-internet-money",
    "SUSD": "nusd",
    "DOLA": "dola-usd",
    "MAI": "mimatic",
    
    # 新兴/合成型
    "USD0": "usd0",
    "USDD": "usdd",
    "USDE": "ethena-usde",
    "USDe": "ethena-usde",  # USDe 和 USDE 指向同一个
}

# 主流稳定币符号集合，便于在交易对中识别两侧稳定币
STABLE_SYMBOLS: set[str] = set(STABLE_SYMBOL_TO_COINGECKO_ID.keys())

# ========== 假币防护：官方合约地址白名单 ==========
# 格式：{symbol: {chain: official_address}}
# 只有在白名单中的合约地址才被认为是真币
OFFICIAL_STABLE_ADDRESSES: dict[str, dict[str, str]] = {
    "USDT": {
        "ethereum": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "bsc": "0x55d398326f99059ff775485246999027b3197955",
        "polygon": "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
        "arbitrum": "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
        "optimism": "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58",
        "base": "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",
        "avalanche": "0x9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7",
    },
    "USDC": {
        "ethereum": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "bsc": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
        "polygon": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
        "arbitrum": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
        "optimism": "0x0b2c639c533813f4aa9d7837caf62653d097ff85",
        "base": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "avalanche": "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e",
    },
    "DAI": {
        "ethereum": "0x6b175474e89094c44da98b954eedeac495271d0f",
        "polygon": "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063",
        "arbitrum": "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1",
        "optimism": "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1",
        "base": "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",
    },
    # 可以继续添加其他稳定币的官方地址
}

# 知名 DEX 白名单（按链分类）
TRUSTED_DEXS: dict[str, set[str]] = {
    "ethereum": {"Uniswap V2", "Uniswap V3", "SushiSwap", "Curve", "Balancer"},
    "bsc": {"PancakeSwap V2", "PancakeSwap V3", "Biswap", "ApeSwap", "THENA"},
    "polygon": {"Uniswap V3", "QuickSwap", "SushiSwap", "Curve", "Balancer"},
    "arbitrum": {"Uniswap V3", "SushiSwap", "Curve", "Camelot", "Balancer"},
    "optimism": {"Uniswap V3", "Velodrome", "Curve", "Balancer"},
    "base": {"Uniswap V3", "Aerodrome", "SushiSwap", "Curve", "BaseSwap"},
    "avalanche": {"Trader Joe", "Pangolin", "Curve", "SushiSwap"},
}

# 自定义稳定币配置文件已在常量部分定义

def load_custom_stable_symbols() -> list[str]:
    """加载自定义稳定币符号列表"""
    if os.path.exists(CUSTOM_STABLE_SYMBOLS_FILE):
        try:
            with open(CUSTOM_STABLE_SYMBOLS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                symbols = [str(s).upper().strip() for s in data if s]
                logger.debug(f"成功加载 {len(symbols)} 个自定义稳定币符号")
                return symbols
        except json.JSONDecodeError as e:
            logger.error(f"自定义稳定币文件 JSON 格式错误: {e}")
        except Exception as e:
            logger.error(f"读取 {CUSTOM_STABLE_SYMBOLS_FILE} 失败: {e}")
    return []

def save_custom_stable_symbols(symbols: list[str]) -> None:
    """保存自定义稳定币符号列表"""
    try:
        os.makedirs(os.path.dirname(CUSTOM_STABLE_SYMBOLS_FILE), exist_ok=True)
        # 去重并转换为大写
        unique_symbols = sorted(list(set([str(s).upper().strip() for s in symbols if s])))
        with open(CUSTOM_STABLE_SYMBOLS_FILE, "w", encoding="utf-8") as f:
            json.dump(unique_symbols, f, ensure_ascii=False, indent=2)
        logger.info(f"已保存 {len(unique_symbols)} 个自定义稳定币符号到 {CUSTOM_STABLE_SYMBOLS_FILE}")
    except Exception as e:
        logger.error(f"保存 {CUSTOM_STABLE_SYMBOLS_FILE} 失败: {e}")

def get_all_stable_symbols() -> list[str]:
    """获取所有稳定币符号（主流 + 自定义）"""
    custom = load_custom_stable_symbols()
    all_symbols = sorted(list(STABLE_SYMBOLS) + custom)
    # 去重
    return sorted(list(set(all_symbols)))


# ========== 假币检测函数 ==========

def is_official_token(symbol: str, chain: str, address: str) -> bool:
    """
    验证代币是否是官方合约地址
    
    参数:
        symbol: 代币符号（如 USDT）
        chain: 链标识（如 ethereum）
        address: 合约地址
    
    返回:
        True 表示是官方地址，False 表示可能是假币
    """
    if not address:
        return False
    
    symbol_upper = symbol.upper()
    address_lower = address.lower()
    
    # 检查是否在白名单中
    if symbol_upper in OFFICIAL_STABLE_ADDRESSES:
        official_addrs = OFFICIAL_STABLE_ADDRESSES[symbol_upper]
        if chain in official_addrs:
            return official_addrs[chain].lower() == address_lower
    
    # 不在白名单中，无法验证（可能是新链或小币种）
    return None  # 返回 None 表示"未知"


def check_token_legitimacy(
    pair_data: dict,
    min_liquidity_usd: float = 50000.0,
    max_price_deviation: float = 0.1,  # 价格偏离 ±10%
) -> dict:
    """
    检查交易对的合法性，识别假币
    
    返回:
        {
            "is_legitimate": bool,  # 是否合法
            "warnings": list[str],  # 警告信息
            "risk_level": str,      # 风险等级：safe/warning/danger
        }
    """
    warnings = []
    risk_level = "safe"
    
    chain = pair_data.get("chain", "").lower()
    base_token = pair_data.get("base_token", {})
    quote_token = pair_data.get("quote_token", {})
    liquidity_usd = pair_data.get("liquidity_usd", 0)
    price_usd = pair_data.get("price_usd")
    
    base_symbol = base_token.get("symbol", "").upper()
    quote_symbol = quote_token.get("symbol", "").upper()
    base_address = base_token.get("address", "")
    quote_address = quote_token.get("address", "")
    
    # 检查1: 流动性过低
    if liquidity_usd < min_liquidity_usd:
        warnings.append(f"⚠️ 流动性过低: ${liquidity_usd:,.0f} < ${min_liquidity_usd:,.0f}")
        risk_level = "warning"
    
    # 检查2: 价格异常（稳定币应该接近 $1）
    if price_usd is not None:
        if abs(price_usd - 1.0) > max_price_deviation:
            warnings.append(f"⚠️ 价格异常: ${price_usd:.4f}（偏离锚定价 {abs(price_usd - 1.0) * 100:.1f}%）")
            risk_level = "danger" if abs(price_usd - 1.0) > 0.5 else "warning"
    
    # 检查3: 验证官方合约地址
    for token_symbol, token_address in [(base_symbol, base_address), (quote_symbol, quote_address)]:
        if token_symbol in OFFICIAL_STABLE_ADDRESSES:
            is_official = is_official_token(token_symbol, chain, token_address)
            if is_official is False:
                warnings.append(f"🚨 假币警告: {token_symbol} 的合约地址不是官方地址！")
                warnings.append(f"   当前地址: {token_address[:10]}...")
                official_addr = OFFICIAL_STABLE_ADDRESSES[token_symbol].get(chain, "未知")
                warnings.append(f"   官方地址: {official_addr[:10] if official_addr != '未知' else official_addr}...")
                risk_level = "danger"
            elif is_official is None:
                warnings.append(f"ℹ️ 无法验证 {token_symbol} 在 {chain} 上的地址（不在白名单）")
    
    # 检查4: DEX 可信度（如果有 dexId 信息）
    dex_id = pair_data.get("dexId", "")
    if dex_id and chain in TRUSTED_DEXS:
        if dex_id not in TRUSTED_DEXS[chain]:
            warnings.append(f"⚠️ 非主流 DEX: {dex_id}")
            risk_level = "warning" if risk_level == "safe" else risk_level
    
    # 综合判断
    is_legitimate = (risk_level != "danger")
    
    return {
        "is_legitimate": is_legitimate,
        "warnings": warnings,
        "risk_level": risk_level,
    }

# 北京时区（UTC+8）
BEIJING_TZ = timezone(timedelta(hours=8))


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def format_beijing(dt: datetime | None = None) -> str:
    if dt is None:
        dt = now_beijing()
    return dt.strftime("%Y-%m-%d %H:%M:%S")

# 示例稳定币配置（请按这个格式填写你真正要监控的池子）
# 关键点：
# - 同一类稳定币在不同链上，用同一个 name，不同的 chain
# - 后面套利扫描会按 name 分组，在这些链之间找 “便宜买 / 贵卖”
DEFAULT_STABLE_CONFIGS: list[dict] = [
    # USDT 多链示例（地址需替换为真实 DexScreener pair 地址最后那串 0x...）
    # {
    #     "name": "USDT",              # 稳定币标识，同一币在不同链保持一致
    #     "chain": "bsc",              # DexScreener 的链标识，如 bsc / arbitrum / base / ethereum
    #     "pair_address": "0x....",    # DexScreener URL 最后一段 0x...（不是合约地址，是 pair 地址）
    #     "anchor_price": 1.0,         # 锚定价，一般 1.0
    #     "threshold": 0.5,            # 脱锚阈值（%），用于“单链脱锚”判断
    # },
    # {
    #     "name": "USDT",
    #     "chain": "arbitrum",
    #     "pair_address": "0x....",
    #     "anchor_price": 1.0,
    #     "threshold": 0.5,
    # },
    # {
    #     "name": "USDT",
    #     "chain": "base",
    #     "pair_address": "0x....",
    #     "anchor_price": 1.0,
    #     "threshold": 0.5,
    # },

    # USDC 多链示例
    # {
    #     "name": "USDC",
    #     "chain": "bsc",
    #     "pair_address": "0x....",
    #     "anchor_price": 1.0,
    #     "threshold": 0.5,
    # },
    # {
    #     "name": "USDC",
    #     "chain": "arbitrum",
    #     "pair_address": "0x....",
    #     "anchor_price": 1.0,
    #     "threshold": 0.5,
    # },
]

# Telegram 全局默认配置（可选，用作缺省值）
DEFAULT_TELEGRAM_BOT_TOKEN = ""   # 可留空
DEFAULT_TELEGRAM_CHAT_ID = ""     # 可留空

# 其它通知渠道全局默认配置（可留空）
DEFAULT_SERVERCHAN_SENDKEY = ""      # Server酱 SendKey
DEFAULT_DINGTALK_WEBHOOK = ""       # 钉钉自定义机器人 Webhook URL


# ========== 配置持久化（CLI & 面板共用） ==========

def load_stable_configs() -> list[dict]:
    """
    从本地 JSON 文件加载稳定币监控配置。
    如果文件不存在或损坏，则回退到代码里的 DEFAULT_STABLE_CONFIGS。
    """
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                logger.debug(f"成功加载 {len(data)} 条稳定币配置")
                return data
            else:
                logger.warning(f"{CONFIG_FILE} 内容格式异常，需为 list，已回退到默认配置")
        except json.JSONDecodeError as e:
            logger.error(f"配置文件 JSON 格式错误: {e}")
        except Exception as e:
            logger.error(f"读取 {CONFIG_FILE} 失败: {e}")
    else:
        logger.info(f"{CONFIG_FILE} 不存在，使用默认配置")
    
    return list(DEFAULT_STABLE_CONFIGS)


def save_stable_configs(configs: list[dict]) -> None:
    """
    将稳定币监控配置保存到本地 JSON 文件，供 CLI 与面板共用。
    """
    try:
        # 确保配置目录存在
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(configs, f, ensure_ascii=False, indent=2)
        logger.info(f"已保存 {len(configs)} 条稳定币配置到 {CONFIG_FILE}")
    except Exception as e:
        logger.error(f"保存 {CONFIG_FILE} 失败: {e}")


# 用户配置文件（多用户通知分发）已在常量部分定义


def load_notify_config() -> dict:
    """
    从本地 JSON 文件加载通知配置（Telegram / Server酱 / 钉钉）。
    如无文件，则回退到代码中的默认值。
    """
    cfg: dict = {
        "telegram_bot_token": DEFAULT_TELEGRAM_BOT_TOKEN,
        "telegram_chat_id": DEFAULT_TELEGRAM_CHAT_ID,
        "serverchan_sendkey": DEFAULT_SERVERCHAN_SENDKEY,
        "dingtalk_webhook": DEFAULT_DINGTALK_WEBHOOK,
    }
    if os.path.exists(NOTIFY_CONFIG_FILE):
        try:
            with open(NOTIFY_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
                logger.debug("成功加载通知配置")
            else:
                logger.warning(f"{NOTIFY_CONFIG_FILE} 内容格式异常，需为 dict")
        except json.JSONDecodeError as e:
            logger.error(f"通知配置文件 JSON 格式错误: {e}")
        except Exception as e:
            logger.error(f"读取 {NOTIFY_CONFIG_FILE} 失败: {e}")
    return cfg


def save_notify_config(cfg: dict) -> None:
    """
    将通知配置保存到本地 JSON 文件，供 CLI 与面板共用。
    """
    try:
        os.makedirs(os.path.dirname(NOTIFY_CONFIG_FILE), exist_ok=True)
        with open(NOTIFY_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logger.info(f"已保存通知配置到 {NOTIFY_CONFIG_FILE}")
    except Exception as e:
        logger.error(f"保存 {NOTIFY_CONFIG_FILE} 失败: {e}")


def load_global_config() -> dict:
    """
    从全局配置文件加载配置，目前主要用于 LI.FI API Key、fromAddress 等。
    """
    cfg: dict = {"lifi_api_key": "", "lifi_from_address": ""}
    if os.path.exists(GLOBAL_CONFIG_FILE):
        try:
            with open(GLOBAL_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
                logger.debug("成功加载全局配置")
            else:
                logger.warning(f"{GLOBAL_CONFIG_FILE} 内容格式异常，需为 dict")
        except json.JSONDecodeError as e:
            logger.error(f"全局配置文件 JSON 格式错误: {e}")
        except Exception as e:
            logger.error(f"读取 {GLOBAL_CONFIG_FILE} 失败: {e}")
    return cfg


def save_global_config(cfg: dict) -> None:
    """
    保存全局配置（目前主要是 LI.FI API Key / fromAddress）。
    """
    try:
        os.makedirs(os.path.dirname(GLOBAL_CONFIG_FILE), exist_ok=True)
        with open(GLOBAL_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logger.info(f"已保存全局配置到 {GLOBAL_CONFIG_FILE}")
    except Exception as e:
        logger.error(f"保存 {GLOBAL_CONFIG_FILE} 失败: {e}")


def load_auth_config() -> dict:
    """
    加载登录配置（用户名、密码）。
    如果文件不存在，创建默认配置。
    使用 PBKDF2 + SHA256 安全加密密码。
    """
    # 使用安全的 PBKDF2 哈希
    default_password_hash, default_salt = hash_password_secure("admin123")
    
    default_config = {
        "username": "admin",
        "password_hash": default_password_hash,
        "salt": default_salt,
    }
    
    if os.path.exists(AUTH_CONFIG_FILE):
        try:
            with open(AUTH_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # 确保必要的字段存在
                if "username" not in data:
                    data["username"] = default_config["username"]
                if "password_hash" not in data:
                    data["password_hash"] = default_password_hash
                if "salt" not in data:
                    # 旧配置没有 salt，重新生成
                    logger.warning("检测到旧版密码格式，正在升级...")
                    data["password_hash"] = default_password_hash
                    data["salt"] = default_salt
                return data
            else:
                print(f"[登录配置] {AUTH_CONFIG_FILE} 内容格式异常，使用默认配置。")
        except Exception as e:
            print(f"[登录配置] 读取 {AUTH_CONFIG_FILE} 失败: {e}，使用默认配置。")
    else:
        # 首次运行，保存默认配置
        save_auth_config(default_config)
        print(f"[登录配置] 已创建默认登录配置，默认用户名: admin，默认密码: admin123")
        print(f"[登录配置] 请及时修改 {AUTH_CONFIG_FILE} 中的密码，或通过面板修改")
    
    return default_config


def save_auth_config(cfg: dict) -> None:
    """
    保存登录配置。
    """
    try:
        os.makedirs(os.path.dirname(AUTH_CONFIG_FILE), exist_ok=True)
        with open(AUTH_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logger.info(f"已保存登录配置到 {AUTH_CONFIG_FILE}")
    except Exception as e:
        logger.error(f"保存 {AUTH_CONFIG_FILE} 失败: {e}")


def check_login() -> bool:
    """
    检查用户是否已登录。
    返回 True 表示已登录，False 表示需要登录。
    """
    # 如果已经认证，直接返回
    if st.session_state.get("authentication_status") == True:
        return True
    
    # 加载登录配置
    config = load_auth_config()
    expected_username = config.get("username", "admin")
    expected_password_hash = config.get("password_hash", "")
    expected_salt = config.get("salt", "")
    
    # 显示登录表单
    st.markdown("## 🔐 登录")
    st.markdown("请输入用户名和密码以访问监控面板")
    
    col1, col2 = st.columns(2)
    with col1:
        username = st.text_input("用户名", value="", key="login_username")
    with col2:
        password = st.text_input("密码", type="password", value="", key="login_password")
    
    if st.button("登录", type="primary", use_container_width=True):
        if not username or not password:
            st.error("请输入用户名和密码")
            return False
        
        # 验证用户名和密码（使用安全的验证方式）
        if username == expected_username and expected_salt:
            is_valid = verify_password_secure(password, expected_password_hash, expected_salt)
        else:
            # 兼容旧版（不推荐）
            password_hash = hashlib.sha256(password.encode()).hexdigest()
            is_valid = password_hash == expected_password_hash
        
        if is_valid:
            # 登录成功
            st.session_state["authentication_status"] = True
            st.session_state["username"] = username
            st.success("登录成功！")
            st.rerun()
        else:
            st.error("用户名或密码不正确")
            return False
    
    return False


def load_users() -> list[dict]:
    """
    从本地 JSON 文件加载用户配置：
    每个用户可以有独立的通知渠道和订阅起止时间。
    """
    if os.path.exists(USERS_CONFIG_FILE):
        try:
            with open(USERS_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                logger.debug(f"成功加载 {len(data)} 个用户配置")
                return data
            else:
                logger.warning(f"{USERS_CONFIG_FILE} 内容格式异常，需为 list")
        except json.JSONDecodeError as e:
            logger.error(f"用户配置文件 JSON 格式错误: {e}")
        except Exception as e:
            logger.error(f"读取 {USERS_CONFIG_FILE} 失败: {e}")
    return []


def save_users(users: list[dict]) -> None:
    """
    将用户配置保存到本地 JSON 文件。
    """
    try:
        os.makedirs(os.path.dirname(USERS_CONFIG_FILE), exist_ok=True)
        with open(USERS_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
        logger.info(f"已保存 {len(users)} 个用户配置到 {USERS_CONFIG_FILE}")
    except Exception as e:
        logger.error(f"保存 {USERS_CONFIG_FILE} 失败: {e}")


def load_collected_pairs_cache() -> list[dict]:
    """
    从本地 JSON 文件加载采集结果缓存。
    如果文件不存在或损坏，返回空列表。
    """
    if os.path.exists(COLLECTED_PAIRS_CACHE_FILE):
        try:
            with open(COLLECTED_PAIRS_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                logger.debug(f"成功加载 {len(data)} 个采集结果缓存")
                return data
            else:
                logger.warning(f"{COLLECTED_PAIRS_CACHE_FILE} 内容格式异常，需为 list")
        except json.JSONDecodeError as e:
            logger.error(f"采集结果缓存文件 JSON 格式错误: {e}")
        except Exception as e:
            logger.error(f"读取 {COLLECTED_PAIRS_CACHE_FILE} 失败: {e}")
    return []


def save_collected_pairs_cache(pairs: list[dict]) -> None:
    """
    将采集结果保存到本地 JSON 文件，实现持久化。
    """
    try:
        os.makedirs(os.path.dirname(COLLECTED_PAIRS_CACHE_FILE), exist_ok=True)
        with open(COLLECTED_PAIRS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(pairs, f, ensure_ascii=False, indent=2)
        logger.info(f"已保存 {len(pairs)} 个采集结果到 {COLLECTED_PAIRS_CACHE_FILE}")
    except Exception as e:
        logger.error(f"保存 {COLLECTED_PAIRS_CACHE_FILE} 失败: {e}")


@cached(ttl=CACHE_TTL_GLOBAL)
def get_coingecko_prices(symbols: list[str]) -> dict[str, float]:
    """
    从 Coingecko 免费 API 获取一批主流稳定币的全局 USD 价格。
    带缓存，减少 API 调用。
    返回: {symbol: price_usd}
    """
    ids: list[str] = []
    symbol_to_id: dict[str, str] = {}
    for sym in symbols:
        key = (sym or "").upper()
        cid = STABLE_SYMBOL_TO_COINGECKO_ID.get(key)
        if not cid:
            continue
        if cid not in ids:
            ids.append(cid)
        symbol_to_id[key] = cid

    if not ids:
        return {}

    for attempt in range(API_RETRY_TIMES):
        try:
            params = {
                "ids": ",".join(ids),
                "vs_currencies": "usd",
            }
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params=params,
                timeout=API_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            
            out: dict[str, float] = {}
            for sym, cid in symbol_to_id.items():
                try:
                    price = float(data.get(cid, {}).get("usd"))
                    if price > 0:
                        out[sym] = price
                except Exception:
                    continue
            return out
            
        except requests.exceptions.Timeout:
            logger.warning(f"Coingecko API 超时 (尝试 {attempt + 1}/{API_RETRY_TIMES})")
            if attempt < API_RETRY_TIMES - 1:
                time.sleep(1)
        except requests.exceptions.HTTPError as e:
            logger.error(f"Coingecko HTTP 错误: {e.response.status_code}")
            return {}
        except Exception as e:
            logger.error(f"Coingecko 获取价格失败: {e}")
            return {}
    
    logger.error(f"Coingecko 获取价格失败，已重试 {API_RETRY_TIMES} 次")
    return {}


def build_pair_crosscheck_text(status: dict) -> str:
    """
    对稳定币-稳定币交易对做交叉核对，用极简文案直接告诉你“哪一侧更可能脱锚”。
    返回形式示例：
      - "（疑似 USDT 脱锚）"
      - "（疑似 USDC 脱锚）"
      - 或空串（无法判断/差不多）
    """
    symbol = (status.get("symbol") or "").upper()
    counter_symbol = (status.get("counter_symbol") or "").upper()
    pool_rate = status.get("pool_rate")
    local_price = status.get("price")

    if not symbol or not counter_symbol or not pool_rate or pool_rate <= 0 or not local_price:
        return ""

    # 只对主流稳定币尝试 cross-check
    syms = [symbol, counter_symbol]
    cg_prices = get_coingecko_prices(syms)
    cg_main = cg_prices.get(symbol)
    cg_counter = cg_prices.get(counter_symbol)
    if not cg_main or not cg_counter:
        return ""

    # 使用 Coingecko 主币价 + 池内汇率推导对手盘隐含价
    # 1 主币 ≈ pool_rate 个对手盘 => 对手盘隐含价 ≈ P_main / pool_rate
    implied_counter = cg_main / float(pool_rate)

    dev_main_local = (float(local_price) - cg_main) / cg_main * 100.0
    dev_counter_implied = (implied_counter - cg_counter) / cg_counter * 100.0

    # 简单判断哪一侧偏离更大，只返回一句话
    if abs(dev_main_local) > abs(dev_counter_implied) * 1.2:
        return f"（疑似 {symbol} 脱锚）"
    elif abs(dev_counter_implied) > abs(dev_main_local) * 1.2:
        return f"（疑似 {counter_symbol} 脱锚）"
    else:
        return ""


def parse_dexscreener_input(
    raw: str, default_chain: str, default_pair: str
) -> tuple[str, str]:
    """
    支持三种输入：
    1) 直接粘贴 DexScreener URL: https://dexscreener.com/base/0x...
    2) 粘贴 'base/0x...' 这样的路径
    3) 只填 pair 地址 '0x...'
    返回 (chain, pair_address)
    """
    raw = (raw or "").strip()
    if not raw:
        return default_chain, default_pair

    # 完整 URL
    if raw.startswith("http://") or raw.startswith("https://"):
        try:
            parsed = urlparse(raw)
            path = (parsed.path or "").strip("/")
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 2:
                return parts[0], parts[1]
            if len(parts) == 1:
                return default_chain or parts[0], parts[0]
        except Exception:
            pass

    # 形如 'base/0x...' 的路径
    if "/" in raw:
        parts = [p for p in raw.split("/") if p]
        if len(parts) >= 2:
            return parts[0], parts[1]

    # 默认视为纯 pair 地址
    return default_chain, raw


def get_available_chains_from_api() -> list[str]:
    """
    通过搜索常见交易对，从 DexScreener API 推断支持的链列表。
    返回链标识列表（小写）。
    """
    # 尝试搜索一些常见交易对，从结果中提取所有出现的链
    test_queries = ["USDT/USDC", "ETH/USDT", "BTC/USDT", "USDC/DAI"]
    chains_found: set[str] = set()
    
    logger.info("[链列表] 正在从 DexScreener API 获取支持的链列表...")
    for query in test_queries:
        try:
            url = "https://api.dexscreener.com/latest/dex/search"
            # 使用速率限制的请求函数
            resp = make_rate_limited_request(
                url,
                params={"q": query},
                timeout=API_TIMEOUT,
                rate_limiter=_dexscreener_rate_limiter,
            )
            data = resp.json()
            
            pairs = data.get("pairs", [])
            for pair in pairs:
                chain_id = pair.get("chainId", "").lower()
                if chain_id:
                    chains_found.add(chain_id)
        except Exception as e:
            logger.warning(f"[链列表] 搜索 {query} 时出错: {e}")
            continue
    
    # 如果 API 没有返回足够的链，合并已知的链列表
    known_chains = set(CHAIN_NAME_TO_ID.keys())
    chains_found = chains_found.union(known_chains)
    
    logger.info(f"[链列表] 找到 {len(chains_found)} 条链")
    
    # 按字母顺序排序
    return sorted(list(chains_found))


def search_stablecoin_pairs(
    stable_symbol: str,
    chains: list[str] | None = None,
    min_liquidity_usd: float = 10000.0,
    max_results_per_chain: int = 5,
) -> list[dict]:
    """
    使用 DexScreener API 自动搜索稳定币交易对。
    
    参数:
        stable_symbol: 稳定币符号（如 "USDT", "USDC"）
        chains: 要搜索的链列表，如果为 None 则搜索所有支持的链
        min_liquidity_usd: 最小流动性要求（USD）
        max_results_per_chain: 每条链最多返回的结果数
    
    返回:
        交易对列表，每项包含：
        {
            "chain": "bsc",
            "pair_address": "0x...",
            "base_token": {"symbol": "USDT", "address": "0x..."},
            "quote_token": {"symbol": "USDC", "address": "0x..."},
            "liquidity_usd": 123456.0,
            "price_usd": 1.001,
        }
    """
    if chains is None:
        chains = list(CHAIN_NAME_TO_ID.keys())
    
    results: list[dict] = []
    
    # 方法1: 使用搜索 API 搜索稳定币交易对
    # 搜索格式: "USDT/USDC", "USDT/DAI" 等
    search_queries = [
        f"{stable_symbol}/USDT",
        f"{stable_symbol}/USDC",
        f"{stable_symbol}/DAI",
        f"{stable_symbol}/BUSD",
        f"{stable_symbol}/USDD",
        f"{stable_symbol}/TUSD",
        f"{stable_symbol}/USDP",
        f"USDT/{stable_symbol}",
        f"USDC/{stable_symbol}",
        f"DAI/{stable_symbol}",
    ]
    
    # 去重，避免重复搜索
    search_queries = list(set(search_queries))
    
    for query_idx, query in enumerate(search_queries, 1):
        try:
            url = "https://api.dexscreener.com/latest/dex/search"
            # 使用速率限制的请求函数
            resp = make_rate_limited_request(
                url,
                params={"q": query},
                timeout=API_TIMEOUT,
                rate_limiter=_dexscreener_rate_limiter,
            )
            data = resp.json()
            
            pairs = data.get("pairs", [])
            for pair in pairs:
                chain_id = pair.get("chainId", "").lower()
                if chain_id not in chains:
                    continue
                
                base_token = pair.get("baseToken", {})
                quote_token = pair.get("quoteToken", {})
                base_symbol = (base_token.get("symbol") or "").upper()
                quote_symbol = (quote_token.get("symbol") or "").upper()
                
                # 只保留稳定币-稳定币交易对
                if base_symbol not in STABLE_SYMBOLS or quote_symbol not in STABLE_SYMBOLS:
                    continue
                
                # 确保至少一侧是我们搜索的稳定币
                if stable_symbol.upper() not in [base_symbol, quote_symbol]:
                    continue
                
                liquidity = pair.get("liquidity", {})
                liquidity_usd = float(liquidity.get("usd", 0) or 0)
                
                if liquidity_usd < min_liquidity_usd:
                    continue
                
                pair_address = pair.get("pairAddress", "")
                if not pair_address:
                    continue
                
                # 检查是否已存在（避免重复）
                existing = any(
                    r.get("chain") == chain_id and r.get("pair_address") == pair_address
                    for r in results
                )
                if existing:
                    continue
                
                price_usd = pair.get("priceUsd")
                try:
                    price_usd = float(price_usd) if price_usd else None
                except Exception:
                    price_usd = None
                
                # 构建交易对数据
                pair_data = {
                    "chain": chain_id,
                    "pair_address": pair_address,
                    "base_token": {
                        "symbol": base_symbol,
                        "address": base_token.get("address", ""),
                    },
                    "quote_token": {
                        "symbol": quote_symbol,
                        "address": quote_token.get("address", ""),
                    },
                    "liquidity_usd": liquidity_usd,
                    "price_usd": price_usd,
                    "dexId": pair.get("dexId", ""),
                }
                
                # 🛡️ 假币检测
                legitimacy = check_token_legitimacy(
                    pair_data,
                    min_liquidity_usd=min_liquidity_usd,
                    max_price_deviation=0.1,
                )
                
                # 添加检测结果到数据中
                pair_data["legitimacy"] = legitimacy
                
                # ⚠️ 如果是危险级别（假币），跳过
                if legitimacy["risk_level"] == "danger":
                    logger.warning(f"检测到假币，已过滤: {base_symbol}/{quote_symbol} on {chain_id}")
                    logger.warning(f"  警告: {', '.join(legitimacy['warnings'])}")
                    continue
                
                results.append(pair_data)
        except Exception as e:
            logger.warning(f"[自动采集] 搜索 {query} 失败: {e}")
            continue
    
    # 方法2: 如果知道稳定币的 token 地址，可以使用 /tokens/v1 API
    # 这里暂时不实现，因为需要预先知道 token 地址
    
    # 按流动性排序，并限制每条链的结果数
    results.sort(key=lambda x: x["liquidity_usd"], reverse=True)
    
    # 按链分组，每条链最多保留 max_results_per_chain 个
    by_chain: dict[str, list[dict]] = {}
    for r in results:
        chain = r["chain"]
        if chain not in by_chain:
            by_chain[chain] = []
        if len(by_chain[chain]) < max_results_per_chain:
            by_chain[chain].append(r)
    
    # 重新组合
    final_results = []
    for chain_results in by_chain.values():
        final_results.extend(chain_results)
    
    return final_results


def auto_collect_stablecoin_pairs(
    stable_symbols: list[str] | None = None,
    chains: list[str] | None = None,
    min_liquidity_usd: float = 10000.0,
    max_results_per_symbol: int = 10,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[list[dict], dict]:
    """
    自动采集多个稳定币的交易对（带速率限制和进度显示）。
    
    参数:
        stable_symbols: 要采集的稳定币符号列表，如果为 None 则使用默认的主流稳定币
        chains: 要搜索的链列表，如果为 None 则搜索所有支持的链
        min_liquidity_usd: 最小流动性要求（USD）
        max_results_per_symbol: 每个稳定币最多返回的结果数
        progress_callback: 进度回调函数，格式为 (current, total, message) -> None
    
    返回:
        (交易对列表, 统计信息字典)
        统计信息包含：
        - total_symbols: 总稳定币数
        - total_pairs_found: 找到的交易对总数（去重前）
        - unique_pairs: 去重后的交易对数
        - errors: 错误数量
        - rate_limit_stats: 速率限制统计
    """
    if stable_symbols is None:
        stable_symbols = list(STABLE_SYMBOLS)
    
    total_symbols = len(stable_symbols)
    all_results: list[dict] = []
    error_count = 0
    
    # 重置速率限制器统计（用于本次采集）
    rate_limiter_stats_before = _dexscreener_rate_limiter.get_stats()
    
    logger.info(f"[自动采集] 开始采集 {total_symbols} 个稳定币的交易对，速率限制: {API_RATE_LIMIT_REQUESTS_PER_SECOND} 次/秒")
    
    for idx, symbol in enumerate(stable_symbols, 1):
        try:
            progress_msg = f"正在搜索 {symbol} 的交易对... ({idx}/{total_symbols})"
            if progress_callback:
                progress_callback(idx, total_symbols, progress_msg)
            else:
                logger.info(f"[自动采集] {progress_msg}")
            
            pairs = search_stablecoin_pairs(
                stable_symbol=symbol,
                chains=chains,
                min_liquidity_usd=min_liquidity_usd,
                max_results_per_chain=max_results_per_symbol,
            )
            all_results.extend(pairs)
            logger.info(f"[自动采集] {symbol} 找到 {len(pairs)} 个交易对")
        except Exception as e:
            error_count += 1
            logger.error(f"[自动采集] 搜索 {symbol} 失败: {e}", exc_info=True)
            if progress_callback:
                progress_callback(idx, total_symbols, f"❌ {symbol} 搜索失败: {str(e)[:50]}")
    
    # 获取速率限制器统计（本次采集后）
    rate_limiter_stats_after = _dexscreener_rate_limiter.get_stats()
    rate_limit_stats = {
        "requests_made": rate_limiter_stats_after["total_requests"] - rate_limiter_stats_before["total_requests"],
        "rate_limited_count": rate_limiter_stats_after["rate_limited_count"] - rate_limiter_stats_before["rate_limited_count"],
    }
    
    # 去重（基于 chain + pair_address）
    seen = set()
    unique_results = []
    for r in all_results:
        key = (r["chain"], r["pair_address"])
        if key not in seen:
            seen.add(key)
            unique_results.append(r)
    
    stats = {
        "total_symbols": total_symbols,
        "total_pairs_found": len(all_results),
        "unique_pairs": len(unique_results),
        "errors": error_count,
        "rate_limit_stats": rate_limit_stats,
    }
    
    logger.info(f"[自动采集] 采集完成: 找到 {len(unique_results)} 个唯一交易对，错误 {error_count} 个，限流 {rate_limit_stats['rate_limited_count']} 次")
    
    return unique_results, stats


# ========== 数据获取与逻辑层 ==========

@cached(ttl=PRICE_CACHE_TTL)
@cached(ttl=CACHE_TTL_PRICE)
def get_dex_price_from_dexscreener(chain: str, pair_address: str) -> float | None:
    """
    从 DexScreener 获取某条链上某个交易对的价格（priceUsd）。
    带短缓存（5秒），快速捕获套利机会。
    文档示例：https://api.dexscreener.com/latest/dex/pairs/{chain}/{pairAddress}
    """
    url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_address}"
    
    for attempt in range(API_RETRY_TIMES):
        try:
            resp = requests.get(url, timeout=API_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()

            pairs = data.get("pairs")
            if not pairs:
                logger.warning(f"DexScreener 无数据: chain={chain}, pair={pair_address}")
                return None

            price_usd = pairs[0].get("priceUsd")
            if price_usd is None:
                logger.warning(f"缺少 priceUsd 字段: chain={chain}, pair={pair_address}")
                return None

            return float(price_usd)
            
        except requests.exceptions.Timeout:
            logger.warning(f"API 超时 (尝试 {attempt + 1}/{API_RETRY_TIMES}): {url}")
            if attempt < API_RETRY_TIMES - 1:
                wait_time = 2 ** attempt
                logger.info(f"等待 {wait_time} 秒后重试...")
                time.sleep(wait_time)
        except (requests.exceptions.ConnectionError, ConnectionResetError, ConnectionAbortedError) as e:
            logger.warning(f"连接错误 (尝试 {attempt + 1}/{API_RETRY_TIMES}): {type(e).__name__} - {url}")
            if attempt < API_RETRY_TIMES - 1:
                wait_time = 2 ** (attempt + 1)
                logger.info(f"检测到连接重置，可能触发限流，等待 {wait_time} 秒后重试...")
                time.sleep(wait_time)
            else:
                logger.error(f"连接持续失败: chain={chain}, pair={pair_address}, err={e}")
                return None
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            if status_code == 429:
                logger.warning(f"API 限流 (429) - (尝试 {attempt + 1}/{API_RETRY_TIMES}): {url}")
                if attempt < API_RETRY_TIMES - 1:
                    wait_time = 5 * (2 ** attempt)
                    logger.info(f"触发限流，等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
            else:
                logger.error(f"HTTP 错误: {status_code} - {url}")
                return None
        except Exception as e:
            logger.error(f"获取 DEX 价格失败: chain={chain}, pair={pair_address}, err={type(e).__name__}: {e}")
            if attempt < API_RETRY_TIMES - 1:
                wait_time = 2 ** attempt
                time.sleep(wait_time)
            else:
                return None
    
    logger.error(f"获取价格失败，已重试 {API_RETRY_TIMES} 次: {url}")
    return None


@cached(ttl=CACHE_TTL_PRICE)
def get_dex_price_and_stable_token(
    chain: str, pair_address: str
) -> tuple[
    float | None,  # pair 里主稳定币的 priceUsd（用于回退）
    str | None,  # 主稳定币地址
    str | None,  # 主稳定币符号
    float | None,  # 池内汇率：1 主稳定币 ≈ pool_rate 个对手盘稳定币
    str | None,  # 对手盘符号
    str | None,  # 对手盘地址
]:
    """
    从 DexScreener 获取价格 + 推断出的稳定币 token 地址 & 符号。
    带缓存，减少 API 调用。
    """
    url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_address}"
    
    for attempt in range(API_RETRY_TIMES):
        try:
            resp = requests.get(url, timeout=API_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()

            pairs = data.get("pairs")
            if not pairs:
                logger.warning(f"DexScreener 无数据: chain={chain}, pair={pair_address}")
                return None, None, None, None, None, None

            pair0 = pairs[0]
            price_usd = pair0.get("priceUsd")
            if price_usd is None:
                logger.warning(f"缺少 priceUsd 字段: chain={chain}, pair={pair_address}")
                return None, None, None, None, None, None

            base = pair0.get("baseToken") or {}
            quote = pair0.get("quoteToken") or {}
            base_symbol = str(base.get("symbol") or "").upper()
            quote_symbol = str(quote.get("symbol") or "").upper()

            liquidity = pair0.get("liquidity") or {}
            liq_base = liquidity.get("base")
            liq_quote = liquidity.get("quote")

            # 优先按主流稳定币来决定"主监控侧"和"对手盘侧"
            # 注意：后续在 fetch_all_stable_status 中会识别两侧的所有 token，不限于主流稳定币
            if base_symbol in STABLE_SYMBOLS:
                stable_token = base
                counter_token = quote
                stable_reserve = liq_base
                counter_reserve = liq_quote
            elif quote_symbol in STABLE_SYMBOLS:
                stable_token = quote
                counter_token = base
                stable_reserve = liq_quote
                counter_reserve = liq_base
            else:
                # 都不是典型稳定币时，默认使用 quoteToken 作为"主监控侧"，baseToken 作为"对手盘侧"
                # 后续会识别两侧，所以这里的区分不影响最终结果
                stable_token = quote or base
                counter_token = base if stable_token is quote else quote
                stable_reserve = liq_quote if stable_token is quote else liq_base
                counter_reserve = liq_base if stable_token is quote else liq_quote

            token_address = stable_token.get("address")
            token_symbol = stable_token.get("symbol")
            counter_symbol = counter_token.get("symbol")
            counter_address = counter_token.get("address")

            pool_rate = None
            try:
                if stable_reserve and counter_reserve and stable_reserve > 0:
                    pool_rate = float(counter_reserve) / float(stable_reserve)
            except Exception:
                pool_rate = None

            return (
                float(price_usd),
                token_address,
                token_symbol,
                pool_rate,
                counter_symbol,
                counter_address,
            )
            
        except requests.exceptions.Timeout:
            logger.warning(f"API 超时 (尝试 {attempt + 1}/{API_RETRY_TIMES}): {url}")
            if attempt < API_RETRY_TIMES - 1:
                # 指数退避：每次重试等待时间递增
                wait_time = 2 ** attempt  # 1s, 2s, 4s...
                logger.info(f"等待 {wait_time} 秒后重试...")
                time.sleep(wait_time)
        except (requests.exceptions.ConnectionError, ConnectionResetError, ConnectionAbortedError) as e:
            # 连接错误：网络问题或被限流
            logger.warning(f"连接错误 (尝试 {attempt + 1}/{API_RETRY_TIMES}): {type(e).__name__} - {url}")
            if attempt < API_RETRY_TIMES - 1:
                # 指数退避 + 额外延迟（连接问题可能是限流）
                wait_time = 2 ** (attempt + 1)  # 2s, 4s, 8s...
                logger.info(f"检测到连接重置，可能触发限流，等待 {wait_time} 秒后重试...")
                time.sleep(wait_time)
            else:
                logger.error(f"连接持续失败: chain={chain}, pair={pair_address}, err={e}")
                return None, None, None, None, None, None
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            if status_code == 429:  # Too Many Requests
                logger.warning(f"API 限流 (429) - (尝试 {attempt + 1}/{API_RETRY_TIMES}): {url}")
                if attempt < API_RETRY_TIMES - 1:
                    wait_time = 5 * (2 ** attempt)  # 5s, 10s, 20s...
                    logger.info(f"触发限流，等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
            else:
                logger.error(f"HTTP 错误: {status_code} - {url}")
                return None, None, None, None, None, None
        except Exception as e:
            logger.error(f"获取 DEX 价格失败: chain={chain}, pair={pair_address}, err={type(e).__name__}: {e}")
            if attempt < API_RETRY_TIMES - 1:
                wait_time = 2 ** attempt
                logger.info(f"未知错误，等待 {wait_time} 秒后重试...")
                time.sleep(wait_time)
            else:
                return None, None, None, None, None, None
    
    logger.error(f"获取价格失败，已重试 {API_RETRY_TIMES} 次: {url}")
    return None, None, None, None, None, None


def _fetch_single_stable_status(
    cfg: dict,
    global_threshold: float | None = None,
) -> list[dict]:
    """
    获取单个配置的稳定币状态（用于并发执行）。
    现在包含流动性检查。
    """
    results: list[dict] = []
    try:
        (
            pair_price,
            token_address,
            token_symbol,
            pool_rate,
            counter_symbol,
            counter_address,
        ) = get_dex_price_and_stable_token(cfg["chain"], cfg["pair_address"])
        if pair_price is None:
            return results
        
        # 获取流动性数据
        liquidity_usd = None
        try:
            url = f"https://api.dexscreener.com/latest/dex/pairs/{cfg['chain']}/{cfg['pair_address']}"
            resp = requests.get(url, timeout=5)
            if resp.ok:
                data = resp.json()
                pairs = data.get("pairs", [])
                if pairs:
                    liquidity = pairs[0].get("liquidity", {})
                    liquidity_usd = liquidity.get("usd")
        except Exception as e:
            logger.debug(f"获取流动性失败: {e}")

        anchor = cfg.get("anchor_price", 1.0)
        # 如果传入了全局阈值，就统一使用全局阈值；否则回退到配置里的值或默认值
        threshold = (
            float(global_threshold)
            if global_threshold is not None
            else float(cfg.get("threshold", DEFAULT_THRESHOLD))
        )

        chain = cfg["chain"]

        # --- 通过 tokens/v1 精确获取两侧稳定币各自的 USD 价格 ---
        token_prices: dict[str, float] = {}
        addrs_to_query: list[str] = []
        addr_symbol_map: dict[str, str] = {}

        if token_address:
            addrs_to_query.append(token_address)
            addr_symbol_map[token_address.lower()] = (token_symbol or "").upper()
        if counter_address:
            addrs_to_query.append(counter_address)
            addr_symbol_map[counter_address.lower()] = (counter_symbol or "").upper()

        if addrs_to_query:
            try:
                url = f"https://api.dexscreener.com/tokens/v1/{chain}/" + ",".join(
                    addrs_to_query
                )
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                data = resp.json()

                # data 是数组，每个元素包含 baseToken/quoteToken/priceUsd/liquidity.usd 等
                # 我们按 tokenAddress 维度聚合，选流动性最大的那个 priceUsd
                best_liq: dict[str, float] = {}
                for item in data or []:
                    liq = float(item.get("liquidity", {}).get("usd") or 0.0)
                    price_usd = item.get("priceUsd")
                    if price_usd is None:
                        continue
                    price_f = float(price_usd)

                    for side in ("baseToken", "quoteToken"):
                        t = item.get(side) or {}
                        addr = str(t.get("address") or "").lower()
                        if addr not in addr_symbol_map:
                            continue
                        if addr not in best_liq or liq > best_liq[addr]:
                            best_liq[addr] = liq
                            token_prices[addr] = price_f
            except Exception as e:
                print(f"[DexScreener tokens.v1] 获取 token 价格失败: chain={chain}, err={e}")

        # 识别主稳定币（第一侧）
        main_symbol = (token_symbol or "").upper()
        main_addr_l = (token_address or "").lower()
        # 优先使用 tokens/v1 的精确价格，如果没有则回退到 pair 的 priceUsd
        main_price = token_prices.get(main_addr_l)
        if main_price is None or main_price <= 0:
            main_price = pair_price
        
        if main_symbol and main_price and main_price > 0:
            deviation_pct = (main_price - anchor) / anchor * 100
            is_alert = abs(deviation_pct) >= threshold
            results.append(
                {
                    "name": main_symbol,
                    "chain": chain,
                    "price": main_price,
                    "deviation_pct": deviation_pct,
                    "threshold": threshold,
                    "is_alert": is_alert,
                    "token_address": token_address,
                    "symbol": main_symbol,
                    "pool_rate": pool_rate,
                    "counter_symbol": counter_symbol,
                    "liquidity_usd": liquidity_usd,  # 流动性（USD）
                }
            )

        # 识别对手盘稳定币（第二侧）- 不再限制为主流稳定币，只要能从 API 获取到价格就识别
        counter_symbol_u = (counter_symbol or "").upper()
        counter_addr_l = (counter_address or "").lower()
        if counter_symbol_u and counter_addr_l:
            # 优先使用 tokens/v1 的精确价格
            counter_price = token_prices.get(counter_addr_l)
            # 如果 tokens/v1 没有价格，尝试通过池内汇率和主币价格推导
            if (counter_price is None or counter_price <= 0) and pool_rate and main_price:
                try:
                    # 1 主币 ≈ pool_rate 个对手盘 => 对手盘价格 ≈ 主币价格 / pool_rate
                    counter_price = main_price / float(pool_rate)
                except Exception:
                    counter_price = None
            
            # 只要获取到有效价格，就添加为监控项
            if counter_price and counter_price > 0:
                counter_deviation = (counter_price - anchor) / anchor * 100
                counter_is_alert = abs(counter_deviation) >= threshold
                results.append(
                    {
                        "name": counter_symbol_u,
                        "chain": chain,
                        "price": counter_price,
                        "deviation_pct": counter_deviation,
                        "threshold": threshold,
                        "is_alert": counter_is_alert,
                        "token_address": counter_address,
                        "symbol": counter_symbol_u,
                        "pool_rate": pool_rate,
                        "counter_symbol": main_symbol,
                        "liquidity_usd": liquidity_usd,  # 流动性（USD）
                    }
                )
    except Exception as e:
        print(f"[错误] 处理配置失败: chain={cfg.get('chain')}, pair={cfg.get('pair_address')}, err={e}")
    return results


def fetch_all_stable_status(
    configs: list[dict],
    global_threshold: float | None = None,
    max_workers: int | None = None,
):
    """
    获取给定配置列表里所有稳定币当前状态（使用并发优化性能）。
    返回列表，每项示例：
    {
        "name": "USDT",
        "chain": "bsc",
        "price": 0.997,
        "deviation_pct": -0.3,
        "threshold": 0.5,
        "is_alert": False,
    }
    
    参数:
        configs: 配置列表
        global_threshold: 全局阈值
        max_workers: 最大并发数（默认根据配置数量动态调整）
    """
    if not configs:
        logger.warning("没有配置需要获取")
        return []
    
    # 动态调整并发数
    if max_workers is None:
        max_workers = min(MAX_CONCURRENT_REQUESTS, max(1, len(configs) // 2))
    
    logger.info(f"开始获取 {len(configs)} 个配置的状态，并发数: {max_workers}")
    
    # 如果配置数量较少，使用顺序执行（避免并发开销）
    if len(configs) <= 5:
        results: list[dict] = []
        for cfg in configs:
            results.extend(_fetch_single_stable_status(cfg, global_threshold))
        logger.info(f"顺序执行完成，获取到 {len(results)} 条状态数据")
        return results
    
    # 使用线程池并发执行
    all_results: list[dict] = []
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # 提交所有任务
        future_to_cfg = {
            executor.submit(_fetch_single_stable_status, cfg, global_threshold): cfg
            for cfg in configs
        }
        
        # 收集结果
        completed = 0
        for future in as_completed(future_to_cfg):
            cfg = future_to_cfg[future]
            completed += 1
            try:
                result = future.result()
                all_results.extend(result)
                logger.debug(f"进度: {completed}/{len(configs)} - {cfg.get('chain')}/{cfg.get('name')}")
            except Exception as e:
                logger.error(f"获取配置结果失败: chain={cfg.get('chain')}, pair={cfg.get('pair_address')}, err={e}")
    
    elapsed = time.time() - start_time
    logger.info(f"并发执行完成，耗时 {elapsed:.2f}秒，获取到 {len(all_results)} 条状态数据")
    
    return all_results


def calculate_arbitrage_cost(
    trade_amount_usd: float,
    src_price: float,
    dst_price: float,
    src_chain: str,
    dst_chain: str,
    src_gas_usd: float,
    dst_gas_usd: float,
    bridge_fee_usd: float,
    slippage_pct: float,
) -> dict:
    """
    计算从 src_chain 买入、跨链到 dst_chain 卖出的套利成本与净利润。
    简化模型：不考虑时间价值，只看单轮往返成本。
    """
    if src_price <= 0 or dst_price <= 0:
        return {
            "理论价差利润": 0.0,
            "总成本": 0.0,
            "Gas费（源链）": 0.0,
            "Gas费（目标链）": 0.0,
            "跨链桥费": 0.0,
            "滑点损失": 0.0,
            "预估净利润": 0.0,
            "预估净利润率": 0.0,
            "价差百分比": 0.0,
        }

    spread_pct = (dst_price - src_price) / src_price * 100
    theoretical_profit = trade_amount_usd * (spread_pct / 100.0)

    slippage_loss = trade_amount_usd * (slippage_pct / 100.0)
    fixed_cost = src_gas_usd + dst_gas_usd + bridge_fee_usd
    total_cost = fixed_cost + slippage_loss
    real_profit = theoretical_profit - total_cost
    profit_margin = (real_profit / trade_amount_usd) * 100.0

    # 估算达到盈亏平衡所需的最低资金规模（只考虑当前 spread 和 slippage 假设）
    # 条件：trade_amount * (spread_pct - slippage_pct)/100 > 固定成本
    # => min_amount = fixed_cost * 100 / (spread_pct - slippage_pct)
    min_trade_amount = None
    effective_edge = spread_pct - slippage_pct
    if effective_edge > 0 and fixed_cost > 0:
        min_trade_amount = fixed_cost * 100.0 / effective_edge

    return {
        "理论价差利润": round(theoretical_profit, 2),
        "总成本": round(total_cost, 2),
        "Gas费（源链）": round(src_gas_usd, 2),
        "Gas费（目标链）": round(dst_gas_usd, 2),
        "跨链桥费": round(bridge_fee_usd, 2),
        "滑点损失": round(slippage_loss, 2),
        "预估净利润": round(real_profit, 2),
        "预估净利润率": round(profit_margin, 3),
        "价差百分比": round(spread_pct, 3),
        "盈亏平衡资金规模": round(min_trade_amount, 2) if min_trade_amount is not None else None,
        "源链": src_chain,
        "目标链": dst_chain,
    }


def get_lifi_supported_chains() -> dict[int, str] | None:
    """
    从 LI.FI API 获取支持的链列表，返回 {chainId: chainKey} 的映射。
    如果请求失败，返回 None。
    """
    try:
        resp = requests.get("https://li.quest/v1/chains", timeout=10)
        if resp.ok:
            data = resp.json()
            chains = data.get("chains", [])
            result: dict[int, str] = {}
            for chain in chains:
                chain_id = chain.get("id")
                chain_key = chain.get("key", "").upper()
                if chain_id and chain_key:
                    result[int(chain_id)] = chain_key
            return result
    except Exception as e:
        print(f"[LI.FI] 获取支持的链列表失败: {e}")
    return None


@cached(ttl=CACHE_TTL_GAS)
def get_lifi_gas_prices(chain_id: int) -> dict[str, float] | None:
    """
    从 LI.FI API 获取指定链的 gas 价格。
    带中等缓存（30秒），Gas 价格相对稳定。
    返回格式: {"standard": float, "fast": float, "fastest": float}
    如果请求失败，返回 None。
    """
    try:
        resp = requests.get(
            f"https://li.quest/v1/gas/prices",
            params={"chainId": chain_id},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            # 根据文档，返回的是 {chainId: {standard, fast, fastest}} 格式
            if isinstance(data, dict):
                chain_data = data.get(str(chain_id)) or data.get(chain_id)
                if chain_data:
                    return {
                        "standard": float(chain_data.get("standard", 0)),
                        "fast": float(chain_data.get("fast", 0)),
                        "fastest": float(chain_data.get("fastest", 0)),
                    }
            # 如果直接返回 gas 价格对象
            if "standard" in data:
                return {
                    "standard": float(data.get("standard", 0)),
                    "fast": float(data.get("fast", 0)),
                    "fastest": float(data.get("fastest", 0)),
                }
    except Exception as e:
        print(f"[LI.FI] 获取链 {chain_id} 的 gas 价格失败: {e}")
    return None


def estimate_gas_cost_usd(chain_id: int, gas_price_gwei: float | None = None, gas_limit: int = 21000) -> float | None:
    """
    估算 gas 费用（USD）。
    
    参数:
        chain_id: 链 ID
        gas_price_gwei: gas 价格（Gwei），如果为 None 则从 LI.FI API 获取
        gas_limit: gas limit，默认 21000（简单转账）
    
    返回:
        估算的 gas 费用（USD），如果无法估算则返回 None
    """
    # 如果未提供 gas 价格，尝试从 LI.FI API 获取
    if gas_price_gwei is None:
        gas_prices = get_lifi_gas_prices(chain_id)
        if gas_prices:
            # 使用 fast 价格作为估算
            gas_price_gwei = gas_prices.get("fast", gas_prices.get("standard", 0))
        else:
            return None
    
    if gas_price_gwei <= 0:
        return None
    
    # 估算 gas 费用（ETH/Gwei）
    # gas_limit * gas_price_gwei / 1e9 = ETH
    gas_cost_eth = (gas_limit * gas_price_gwei) / 1e9
    
    # 获取 ETH 价格（简化处理，使用固定值或从 Coingecko 获取）
    # 这里先使用固定值 2500 USD，实际应该从价格 API 获取
    eth_price_usd = 2500.0  # 可以后续改进为动态获取
    
    return gas_cost_eth * eth_price_usd


def _guess_decimals_from_symbol(symbol: str | None) -> int:
    """
    粗略根据符号猜测小数位：
    - USDT / USDC 系一般是 6 位
    - 其它稳定币默认按 18 位处理
    """
    if not symbol:
        return 18
    sym = symbol.upper()
    if sym in {"USDT", "USDC", "USDT.E", "USDC.E"}:
        return 6
    return 18


def refine_cost_with_lifi(
    src_status: dict,
    dst_status: dict,
    trade_amount_usd: float,
    base_cost_detail: dict,
) -> dict:
    """
    使用 LI.FI quote 对某个跨链机会做二次精算。
    输入：
      - src_status / dst_status: 来自 fetch_all_stable_status 的状态，要求包含 chain / price / token_address / symbol
      - trade_amount_usd: 计划套利资金规模
      - base_cost_detail: 原先基于简单假设算出的成本 dict

    返回：
      - 覆盖了“预估净利润 / 预估净利润率 / 总成本”的新 dict（如请求失败则原样返回）
    """
    try:
        src_chain = str(src_status["chain"])
        dst_chain = str(dst_status["chain"])
        src_chain_id = CHAIN_NAME_TO_ID.get(src_chain)
        dst_chain_id = CHAIN_NAME_TO_ID.get(dst_chain)
        src_token = src_status.get("token_address")
        dst_token = dst_status.get("token_address")
        src_symbol = src_status.get("symbol")
        dst_symbol = dst_status.get("symbol")

        # 详细检查每个必要参数，并输出调试信息
        missing_items = []
        if not src_chain_id:
            missing_items.append(f"源链 '{src_chain}' 不在 chainId 映射表中")
        if not dst_chain_id:
            missing_items.append(f"目标链 '{dst_chain}' 不在 chainId 映射表中")
        if not src_token:
            missing_items.append(f"源 token 地址为空（symbol: {src_symbol}）")
        if not dst_token:
            missing_items.append(f"目标 token 地址为空（symbol: {dst_symbol}）")
        
        if missing_items:
            skip_reason = "；".join(missing_items)
            print(f"[LI.FI 精算跳过] 缺少必要参数：{skip_reason}，使用基础成本估算。")
            # 在返回的 cost_detail 中添加跳过原因，供面板显示
            base_cost_detail["LI.FI_跳过原因"] = skip_reason
            return base_cost_detail

        # 注意：不再提前检查链是否在支持列表中，而是直接尝试调用 API
        # 让 LI.FI API 自己判断是否支持该链，这样更准确，也能自动适配新链

        # 检查源链和目标链是否相同（相同链不需要跨链）
        if src_chain_id == dst_chain_id:
            skip_reason = f"源链和目标链相同（{src_chain}），无需跨链"
            print(f"[LI.FI 精算跳过] {skip_reason}，使用基础成本估算。")
            base_cost_detail["LI.FI_跳过原因"] = skip_reason
            return base_cost_detail

        # 检查源 token 和目标 token 地址是否相同（LI.FI 不允许相同 token）
        src_token_lower = str(src_token).lower().strip()
        dst_token_lower = str(dst_token).lower().strip()
        if src_token_lower == dst_token_lower:
            skip_reason = f"源 token 和目标 token 地址相同（{src_token_lower[:10]}...），LI.FI 不支持相同 token 的跨链"
            print(f"[LI.FI 精算跳过] {skip_reason}，使用基础成本估算。")
            base_cost_detail["LI.FI_跳过原因"] = skip_reason
            return base_cost_detail

        src_price = float(src_status["price"])
        dst_price = float(dst_status["price"])
        if src_price <= 0 or dst_price <= 0:
            skip_reason = "价格数据无效"
            base_cost_detail["LI.FI_跳过原因"] = skip_reason
            return base_cost_detail

        # 资金规模换算成源链稳定币数量和整数 fromAmount
        src_decimals = _guess_decimals_from_symbol(src_symbol)
        dst_decimals = _guess_decimals_from_symbol(dst_symbol)

        src_amount_tokens = trade_amount_usd / src_price
        from_amount_int = int(src_amount_tokens * (10**src_decimals))

        # 读取全局配置：API Key + fromAddress
        headers: dict[str, str] = {}
        from_address = ""
        try:
            gcfg = load_global_config()
            api_key = gcfg.get("lifi_api_key") or os.environ.get("LIFI_API_KEY", "")
            from_address_raw = gcfg.get("lifi_from_address") or ""
            # 确保 from_address 是字符串且去除首尾空白
            from_address = str(from_address_raw).strip() if from_address_raw else ""
            if api_key:
                headers["x-lifi-api-key"] = api_key
        except Exception as e:
            print(f"[LI.FI 配置] 读取全局配置失败: {e}")
            from_address = ""

        # 严格检查：from_address 必须是非空字符串（至少是有效的以太坊地址格式）
        if not from_address or len(from_address) < 10:
            # 未配置 fromAddress 时，直接跳过精算，避免 400 错误
            skip_reason = "未设置 fromAddress（请在左侧面板的全局设置中配置 LI.FI fromAddress）"
            print(f"[LI.FI 配置] {skip_reason}，跳过 LI.FI 实时报价，仅使用面板参数估算成本。")
            base_cost_detail["LI.FI_跳过原因"] = skip_reason
            return base_cost_detail

        # LI.FI API 可能需要 chainId 为字符串格式，确保转换
        params = {
            "fromChain": str(src_chain_id),
            "toChain": str(dst_chain_id),
            "fromToken": src_token,
            "toToken": dst_token,
            "fromAmount": str(from_amount_int),
            "fromAddress": from_address,
        }

        # 调试信息：打印请求参数（不包含敏感信息）
        print(f"[LI.FI 调试] 请求参数: fromChain={src_chain_id}({src_chain}), toChain={dst_chain_id}({dst_chain}), "
              f"fromToken={src_token[:10]}..., toToken={dst_token[:10]}...")

        try:
            resp = requests.get(
                "https://li.quest/v1/quote",
                params=params,
                headers=headers or None,
                timeout=15,
            )
            if not resp.ok:
                error_text = str(resp.text)[:500]  # 增加错误文本长度，获取更多信息
                # 尝试解析错误响应，判断是否是链不支持
                try:
                    error_data = resp.json()
                    error_message = error_data.get("message", "")
                    error_code = error_data.get("code", "")
                    
                    # 检查是否是链不支持的错误
                    if "not supported" in error_message.lower() or "unsupported" in error_message.lower():
                        skip_reason = f"LI.FI 不支持该链对（{src_chain}({src_chain_id}) -> {dst_chain}({dst_chain_id})）: {error_message}"
                    elif "must be equal to one of the allowed values" in error_message.lower() or "must match exactly one schema" in error_message.lower():
                        # 这是链 ID 不在允许列表中的错误
                        # 尝试从 LI.FI API 获取支持的链列表，提供更详细的错误信息
                        supported_chains = get_lifi_supported_chains()
                        supported_chain_ids = list(supported_chains.keys()) if supported_chains else []
                        
                        if "/toChain" in error_message:
                            if supported_chains and dst_chain_id in supported_chains:
                                # chainId 在支持列表中，可能是其他问题
                                skip_reason = (
                                    f"LI.FI API 拒绝目标链 '{dst_chain}' (chainId: {dst_chain_id})，"
                                    f"虽然该 chainId 在支持的列表中，但可能不支持该链对或 token。"
                                    f"错误详情: {error_message}"
                                )
                            else:
                                skip_reason = (
                                    f"LI.FI 不支持目标链 '{dst_chain}' (chainId: {dst_chain_id})。"
                                )
                                if supported_chain_ids:
                                    skip_reason += f" LI.FI 支持的 chainId 包括: {', '.join(map(str, sorted(supported_chain_ids)[:20]))}..."
                                skip_reason += f" 详情请查看: https://docs.li.fi/"
                        elif "/fromChain" in error_message:
                            if supported_chains and src_chain_id in supported_chains:
                                skip_reason = (
                                    f"LI.FI API 拒绝源链 '{src_chain}' (chainId: {src_chain_id})，"
                                    f"虽然该 chainId 在支持的列表中，但可能不支持该链对或 token。"
                                    f"错误详情: {error_message}"
                                )
                            else:
                                skip_reason = (
                                    f"LI.FI 不支持源链 '{src_chain}' (chainId: {src_chain_id})。"
                                )
                                if supported_chain_ids:
                                    skip_reason += f" LI.FI 支持的 chainId 包括: {', '.join(map(str, sorted(supported_chain_ids)[:20]))}..."
                                skip_reason += f" 详情请查看: https://docs.li.fi/"
                        else:
                            skip_reason = (
                                f"LI.FI 不支持该链对（{src_chain}({src_chain_id}) -> {dst_chain}({dst_chain_id})）。"
                            )
                            if supported_chain_ids:
                                skip_reason += f" LI.FI 支持的 chainId 包括: {', '.join(map(str, sorted(supported_chain_ids)[:20]))}..."
                            skip_reason += f" 详情请查看: https://docs.li.fi/"
                    elif error_code == 1011 and "same token" in error_message.lower():
                        skip_reason = f"源 token 和目标 token 相同，LI.FI 不支持: {error_message}"
                    else:
                        skip_reason = f"LI.FI API 请求失败（HTTP {resp.status_code}, code {error_code}）: {error_message}"
                except Exception:
                    # 如果无法解析 JSON，使用原始错误文本
                    skip_reason = f"LI.FI API 请求失败（HTTP {resp.status_code}）: {error_text}"
                
                print(f"[LI.FI 精算失败] {skip_reason}")
                base_cost_detail["LI.FI_跳过原因"] = skip_reason
                return base_cost_detail
            data = resp.json()
        except Exception as e:
            skip_reason = f"LI.FI API 请求异常: {str(e)}"
            print(f"[LI.FI 精算异常] {skip_reason}")
            base_cost_detail["LI.FI_跳过原因"] = skip_reason
            return base_cost_detail

        estimate = data.get("estimate") or {}
        to_amount_str = estimate.get("toAmount")
        if not to_amount_str:
            skip_reason = "LI.FI API 响应中缺少 estimate.toAmount 字段"
            print(f"[LI.FI 精算失败] {skip_reason}")
            base_cost_detail["LI.FI_跳过原因"] = skip_reason
            return base_cost_detail

        to_amount_int = int(to_amount_str)
        dst_amount_tokens = to_amount_int / (10**dst_decimals)
        
        # 计算理论应该收到的数量（不考虑滑点和费用）
        # 理论数量 = 投入数量 * (目标链价格 / 源链价格)
        src_amount_tokens = trade_amount_usd / src_price
        theoretical_dst_tokens = src_amount_tokens * (dst_price / src_price)
        theoretical_dst_usd = theoretical_dst_tokens * dst_price
        
        # 计算实际滑点损失（理论金额 - 实际到手金额）
        actual_revenue_usd = dst_amount_tokens * dst_price
        slippage_loss_from_lifi = theoretical_dst_usd - actual_revenue_usd
        
        # 计算滑点百分比
        slippage_pct_from_lifi = None
        if theoretical_dst_usd > 0:
            slippage_pct_from_lifi = (slippage_loss_from_lifi / theoretical_dst_usd) * 100.0

        # 从 LI.FI quote 响应中提取所有费用信息
        # estimate 可能包含：gasCosts, feeCosts, tool, steps 等
        gas_costs = estimate.get("gasCosts", [])
        fee_costs = estimate.get("feeCosts", [])
        
        src_gas_from_lifi = None
        dst_gas_from_lifi = None
        bridge_fee_from_lifi = None
        total_fees_from_lifi = None
        
        # 提取 gas 费用
        if gas_costs:
            for gas_cost in gas_costs:
                chain_id = gas_cost.get("chainId")
                token = gas_cost.get("token", {})
                amount = gas_cost.get("amount")
                price_usd = token.get("priceUSD")
                
                if amount and price_usd:
                    try:
                        decimals = token.get("decimals", 18)
                        amount_float = float(amount) / (10 ** decimals)
                        gas_usd = amount_float * float(price_usd)
                        
                        if chain_id == src_chain_id:
                            src_gas_from_lifi = gas_usd
                        elif chain_id == dst_chain_id:
                            dst_gas_from_lifi = gas_usd
                    except Exception:
                        pass
        
        # 提取手续费和跨链桥费用
        # feeCosts 可能包含跨链桥费用、协议费用等
        if fee_costs:
            bridge_fees = []
            other_fees = []
            
            for fee_cost in fee_costs:
                token = fee_cost.get("token", {})
                amount = fee_cost.get("amount")
                price_usd = token.get("priceUSD")
                name = fee_cost.get("name", "").lower()
                
                if amount and price_usd:
                    try:
                        decimals = token.get("decimals", 18)
                        amount_float = float(amount) / (10 ** decimals)
                        fee_usd = amount_float * float(price_usd)
                        
                        # 判断是否是跨链桥费用
                        if "bridge" in name or "cross" in name or "transfer" in name:
                            bridge_fees.append(fee_usd)
                        else:
                            other_fees.append(fee_usd)
                    except Exception:
                        pass
            
            if bridge_fees:
                bridge_fee_from_lifi = sum(bridge_fees)
            if other_fees:
                total_fees_from_lifi = sum(other_fees)
        
        # 如果没有从 feeCosts 中获取到桥费，尝试从 steps 中提取
        # LI.FI 的路由可能包含多个步骤，每个步骤可能有费用
        steps = data.get("steps", [])
        if not bridge_fee_from_lifi and steps:
            for step in steps:
                step_estimate = step.get("estimate", {})
                step_fee_costs = step_estimate.get("feeCosts", [])
                step_tool = step.get("tool", "")
                
                # 如果工具是桥，则费用可能是桥费
                if "bridge" in step_tool.lower() and step_fee_costs:
                    for fee_cost in step_fee_costs:
                        token = fee_cost.get("token", {})
                        amount = fee_cost.get("amount")
                        price_usd = token.get("priceUSD")
                        
                        if amount and price_usd:
                            try:
                                decimals = token.get("decimals", 18)
                                amount_float = float(amount) / (10 ** decimals)
                                fee_usd = amount_float * float(price_usd)
                                if bridge_fee_from_lifi is None:
                                    bridge_fee_from_lifi = 0
                                bridge_fee_from_lifi += fee_usd
                            except Exception:
                                pass
        
        # 如果没有从 quote 中获取到 gas 费用，尝试从 gas/prices API 获取
        if src_gas_from_lifi is None:
            src_gas_prices = get_lifi_gas_prices(src_chain_id)
            if src_gas_prices:
                # 使用 fast 价格估算，假设 gas limit 为 100000（DEX 交易通常需要更多 gas）
                estimated_src_gas = estimate_gas_cost_usd(
                    src_chain_id, 
                    gas_price_gwei=src_gas_prices.get("fast"),
                    gas_limit=100000  # DEX swap 通常需要更多 gas
                )
                if estimated_src_gas:
                    src_gas_from_lifi = estimated_src_gas
        
        if dst_gas_from_lifi is None:
            dst_gas_prices = get_lifi_gas_prices(dst_chain_id)
            if dst_gas_prices:
                estimated_dst_gas = estimate_gas_cost_usd(
                    dst_chain_id,
                    gas_price_gwei=dst_gas_prices.get("fast"),
                    gas_limit=100000
                )
                if estimated_dst_gas:
                    dst_gas_from_lifi = estimated_dst_gas

        # 以目标链稳定币价格估算最终拿到的 USD
        revenue_usd = dst_amount_tokens * dst_price
        real_profit = revenue_usd - trade_amount_usd
        profit_margin = (real_profit / trade_amount_usd) * 100.0

        # 用价差模型的理论利润 - 实际利润 来近似总成本
        spread_pct = (dst_price - src_price) / src_price * 100
        theoretical_profit = trade_amount_usd * (spread_pct / 100.0)
        total_cost_est = theoretical_profit - real_profit

        refined = dict(base_cost_detail)
        refined["理论价差利润"] = round(theoretical_profit, 2)
        refined["总成本"] = round(total_cost_est, 2)
        refined["预估净利润"] = round(real_profit, 2)
        refined["预估净利润率"] = round(profit_margin, 3)
        refined["LI.FI_到手数量"] = round(dst_amount_tokens, 6)
        refined["LI.FI_数据来源"] = "li.quest quote"
        
        # 使用从 LI.FI 获取的所有费用信息更新成本明细
        updated_src_gas = base_cost_detail.get("Gas费（源链）", 0)
        updated_dst_gas = base_cost_detail.get("Gas费（目标链）", 0)
        updated_bridge_fee = base_cost_detail.get("跨链桥费", 0)
        updated_slippage_loss = base_cost_detail.get("滑点损失", 0)
        
        # 更新 gas 费用
        if src_gas_from_lifi is not None:
            updated_src_gas = src_gas_from_lifi
            refined["Gas费（源链）"] = round(src_gas_from_lifi, 2)
            refined["LI.FI_源链Gas来源"] = "LI.FI API"
        if dst_gas_from_lifi is not None:
            updated_dst_gas = dst_gas_from_lifi
            refined["Gas费（目标链）"] = round(dst_gas_from_lifi, 2)
            refined["LI.FI_目标链Gas来源"] = "LI.FI API"
        
        # 更新跨链桥费用
        if bridge_fee_from_lifi is not None:
            updated_bridge_fee = bridge_fee_from_lifi
            refined["跨链桥费"] = round(bridge_fee_from_lifi, 2)
            refined["LI.FI_跨链桥费来源"] = "LI.FI API"
        
        # 添加其他手续费（如果有）
        if total_fees_from_lifi is not None and total_fees_from_lifi > 0:
            refined["其他手续费"] = round(total_fees_from_lifi, 2)
            refined["LI.FI_其他手续费来源"] = "LI.FI API"
        
        # 更新滑点损失（从 LI.FI 实际路由中计算）
        # 总损失 = 理论应该收到的金额 - 实际收到的金额
        # 这个总损失包含了所有成本：gas、桥费、手续费、滑点等
        # 滑点损失 = 总损失 - 其他费用（gas、桥费、手续费）
        if slippage_loss_from_lifi > 0:
            # 从总损失中减去已知的费用，得到纯滑点损失
            known_costs = (src_gas_from_lifi or 0) + (dst_gas_from_lifi or 0) + (bridge_fee_from_lifi or 0) + (total_fees_from_lifi or 0)
            pure_slippage_loss = max(0, slippage_loss_from_lifi - known_costs)
            
            if pure_slippage_loss > 0:
                updated_slippage_loss = pure_slippage_loss
                refined["滑点损失"] = round(pure_slippage_loss, 2)
                refined["LI.FI_滑点损失来源"] = "LI.FI API"
                
                # 计算基于实际滑点损失的滑点百分比
                # 滑点百分比 = (滑点损失 / 理论应该收到的金额) * 100
                if theoretical_dst_usd > 0:
                    actual_slippage_pct = (pure_slippage_loss / theoretical_dst_usd) * 100.0
                    refined["滑点百分比"] = round(actual_slippage_pct, 3)
                    refined["LI.FI_滑点百分比来源"] = "LI.FI API"
        
        # 重新计算总成本（使用从 LI.FI 获取的所有费用）
        total_cost_from_lifi = updated_src_gas + updated_dst_gas + updated_bridge_fee + updated_slippage_loss
        if total_fees_from_lifi is not None:
            total_cost_from_lifi += total_fees_from_lifi
        
        # 如果从 LI.FI 获取到了费用信息，使用更准确的总成本
        if (src_gas_from_lifi is not None or dst_gas_from_lifi is not None or 
            bridge_fee_from_lifi is not None or total_fees_from_lifi is not None or
            (slippage_loss_from_lifi > 0 and updated_slippage_loss != base_cost_detail.get("滑点损失", 0))):
            refined["总成本"] = round(total_cost_from_lifi, 2)
            refined["预估净利润"] = round(theoretical_profit - total_cost_from_lifi, 2)
            refined["预估净利润率"] = round((refined["预估净利润"] / trade_amount_usd) * 100.0, 3)
            
            # 标记使用了 LI.FI 的完整费用数据
            refined["LI.FI_费用数据完整"] = True
        
        # 这里不重新计算盈亏平衡资金规模，保留基于简化成本模型的估算值
        return refined
    except Exception as e:
        skip_reason = f"LI.FI 精算过程异常: {str(e)}"
        print(f"[LI.FI 精算失败] {skip_reason}")
        base_cost_detail["LI.FI_跳过原因"] = skip_reason
        return base_cost_detail


# ========== 跨链套利机会扫描 ==========

def find_arbitrage_opportunities(
    statuses: list[dict],
    trade_amount_usd: float = DEFAULT_TRADE_AMOUNT_USD,
    src_gas_usd: float = DEFAULT_SRC_GAS_USD,
    dst_gas_usd: float = DEFAULT_DST_GAS_USD,
    bridge_fee_usd: float = DEFAULT_BRIDGE_FEE_USD,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    min_profit_usd: float = DEFAULT_MIN_PROFIT_USD,
    min_profit_rate: float = DEFAULT_MIN_PROFIT_RATE,
    min_spread_pct: float = DEFAULT_MIN_SPREAD_PCT,
) -> list[dict]:
    """
    在同一 name 的不同链之间，寻找可能的跨链套利机会。

    返回列表，每项结构大致为：
    {
        "name": "USDT",
        "cheap_chain": "bsc",
        "cheap_price": 0.9975,
        "rich_chain": "arbitrum",
        "rich_price": 1.0012,
        "cost_detail": {...}  # calculate_arbitrage_cost 的结果
    }
    """
    from collections import defaultdict

    by_name: dict[str, list[dict]] = defaultdict(list)
    for s in statuses:
        by_name[s["name"]].append(s)

    opps: list[dict] = []

    for name, lst in by_name.items():
        if len(lst) < 2:
            continue  # 只有一条链，没有跨链可言

        # 找到最便宜和最贵的一条链
        cheap = min(lst, key=lambda x: x["price"])
        rich = max(lst, key=lambda x: x["price"])
        if rich["price"] <= cheap["price"]:
            continue

        # 先看价差是否够大
        spread_pct = (rich["price"] - cheap["price"]) / cheap["price"] * 100
        if spread_pct < min_spread_pct:
            continue
        
        # 检查流动性（确保能成交）
        cheap_liq = cheap.get("liquidity_usd")
        rich_liq = rich.get("liquidity_usd")
        if cheap_liq is not None and cheap_liq < MIN_LIQUIDITY_USD:
            logger.debug(f"跳过低流动性池子: {name} ({cheap['chain']}) 流动性=${cheap_liq:.0f}")
            continue
        if rich_liq is not None and rich_liq < MIN_LIQUIDITY_USD:
            logger.debug(f"跳过低流动性池子: {name} ({rich['chain']}) 流动性=${rich_liq:.0f}")
            continue

        # 按当前默认参数估算实际净利润（初步筛选）
        cost_detail = calculate_arbitrage_cost(
            trade_amount_usd=trade_amount_usd,
            src_price=cheap["price"],
            dst_price=rich["price"],
            src_chain=cheap["chain"],
            dst_chain=rich["chain"],
            src_gas_usd=src_gas_usd,
            dst_gas_usd=dst_gas_usd,
            bridge_fee_usd=bridge_fee_usd,
            slippage_pct=slippage_pct,
        )

        # 使用 LI.FI quote 做二次精算（成功则覆盖净利润相关字段）
        cost_detail = refine_cost_with_lifi(
            src_status=cheap,
            dst_status=rich,
            trade_amount_usd=trade_amount_usd,
            base_cost_detail=cost_detail,
        )

        net_profit = cost_detail.get("预估净利润", 0.0)
        net_margin = cost_detail.get("预估净利润率", 0.0)

        if net_profit < min_profit_usd or net_margin < min_profit_rate:
            continue

        opps.append(
            {
                "name": name,
                "cheap_chain": cheap["chain"],
                "cheap_price": cheap["price"],
                "rich_chain": rich["chain"],
                "rich_price": rich["price"],
                "cost_detail": cost_detail,
            }
        )

    # 按净利润从高到低排序
    opps.sort(key=lambda x: x["cost_detail"]["预估净利润"], reverse=True)
    return opps


# ========== 通知层（Telegram） ==========

def load_send_log() -> list[dict]:
    """加载发送日志"""
    if os.path.exists(SEND_LOG_FILE):
        try:
            with open(SEND_LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception as e:
            logger.error(f"读取发送日志失败: {e}")
    return []


def save_send_log(logs: list[dict]) -> None:
    """保存发送日志"""
    try:
        os.makedirs(os.path.dirname(SEND_LOG_FILE), exist_ok=True)
        # 只保留最近100条
        if len(logs) > 100:
            logs = logs[-100:]
        with open(SEND_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存发送日志失败: {e}")


def add_send_log(msg_type: str, content: str, channels: list[str], success: bool = True):
    """添加发送日志"""
    logs = load_send_log()
    logs.append({
        "time": format_beijing(),
        "type": msg_type,
        "content": content[:100],  # 只保存前100字符
        "channels": channels,
        "success": success
    })
    save_send_log(logs)
    logger.info(f"发送日志: {msg_type} - {channels} - {'成功' if success else '失败'}")


def get_today_send_count(channel: str | None = None) -> int:
    """
    获取今天已发送的消息数量
    
    参数:
        channel: 如果指定，只统计该渠道的发送次数；如果为 None，只统计 Server酱 的发送次数
    """
    logs = load_send_log()
    today = now_beijing().strftime("%Y-%m-%d")
    count = 0
    for log in logs:
        if log.get("time", "").startswith(today) and log.get("success"):
            channels = log.get("channels", [])
            if channel:
                # 统计指定渠道
                if channel in channels:
                    count += 1
            else:
                # 默认只统计 Server酱（因为只有 Server酱 有限制）
                if "Server酱" in channels:
                    count += 1
    return count


def can_send_serverchan() -> bool:
    """检查今天是否还能通过 Server酱 发送消息（Server酱每天限制5条）"""
    return get_today_send_count("Server酱") < MAX_DAILY_SENDS


def can_send_today() -> bool:
    """
    检查今天是否还能发送消息（兼容旧代码，实际只检查 Server酱）
    注意：Telegram 和钉钉没有限制，可以随时发送
    """
    return can_send_serverchan()


def should_send_heartbeat() -> bool:
    """
    检查是否应该发送心跳（每天 12:00 固定时间）
    返回 True 表示现在应该发送
    """
    now = now_beijing()
    current_hour = now.hour
    current_minute = now.minute
    
    # 检查是否在 12:00-12:30 之间
    if not (current_hour == 12 and 0 <= current_minute < 30):
        return False
    
    # 检查今天是否已发送过心跳
    logs = load_send_log()
    today = now.strftime("%Y-%m-%d")
    
    # 查找今天的心跳发送记录
    for log in reversed(logs):  # 从最新的开始查
        log_time = log.get("time", "")
        if log_time.startswith(today):
            if log.get("type") == "心跳" and log.get("success"):
                return False  # 今天已发送过
    
    return True  # 今天未发送且在时间窗口内


def send_telegram(text: str, bot_token: str, chat_id: str):
    if not bot_token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=5)
        return resp.ok
    except Exception as e:
        logger.error(f"Telegram 发送失败: {e}")
        return False


def send_serverchan(text: str, sendkey: str):
    """
    通过 Server酱发送通知。
    文档：https://sct.ftqq.com/
    """
    if not sendkey:
        return False
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    try:
        resp = requests.post(url, data={"title": "稳定币监控通知", "desp": text}, timeout=5)
        return resp.ok
    except Exception as e:
        logger.error(f"Server酱 发送失败: {e}")
        return False


def send_dingtalk(text: str, webhook: str):
    """
    通过钉钉自定义机器人发送文本消息。
    """
    if not webhook:
        return False
    try:
        resp = requests.post(
            webhook,
            json={"msgtype": "text", "text": {"content": text}},
            timeout=5,
        )
        return resp.ok
    except Exception as e:
        logger.error(f"钉钉 发送失败: {e}")
        return False


def send_all_notifications(text: str, notify_cfg: dict | None = None, msg_type: str = "通知"):
    """
    多渠道发送通知：Telegram / Server酱 / 钉钉。
    带额度管理和日志记录。
    
    注意：
    - Server酱：每天限制 5 条（免费版限制）
    - Telegram：无限制，可随时发送
    - 钉钉：无限制，可随时发送
    
    参数:
        text: 通知内容
        notify_cfg: 通知配置（测试用）
        msg_type: 消息类型（用于日志）
    """
    sent_channels = []
    success = False
    
    # 如果显式传入了 notify_cfg（例如面板测试按钮），仅按这套配置发送一次
    if notify_cfg is not None:
        tg_token = notify_cfg.get("telegram_bot_token") or DEFAULT_TELEGRAM_BOT_TOKEN
        tg_chat = notify_cfg.get("telegram_chat_id") or DEFAULT_TELEGRAM_CHAT_ID
        sc_key = notify_cfg.get("serverchan_sendkey") or DEFAULT_SERVERCHAN_SENDKEY
        dt_hook = notify_cfg.get("dingtalk_webhook") or DEFAULT_DINGTALK_WEBHOOK

        # Telegram 无限制，直接发送
        if tg_token and tg_chat:
            if send_telegram(text, tg_token, tg_chat):
                sent_channels.append("Telegram")
                success = True
        
        # Server酱 需要检查额度
        if sc_key:
            if can_send_serverchan():
                if send_serverchan(text, sc_key):
                    sent_channels.append("Server酱")
                    success = True
            else:
                logger.warning(f"Server酱今日额度已用完（{MAX_DAILY_SENDS}条），跳过发送")
        
        # 钉钉 无限制，直接发送
        if dt_hook:
            if send_dingtalk(text, dt_hook):
                sent_channels.append("钉钉")
                success = True
        
        add_send_log("测试", text, sent_channels, success)
        return success

    # 未显式传入配置：优先按用户列表（users.json）分发
    users = load_users()
    active_users: list[dict] = []
    now = datetime.utcnow()
    for user in users:
        try:
            if not user.get("enabled", True):
                continue
            start_str = user.get("start_at") or ""
            end_str = user.get("end_at") or ""
            ok_time = True
            if start_str:
                try:
                    if now < datetime.fromisoformat(start_str):
                        ok_time = False
                except Exception:
                    pass
            if end_str:
                try:
                    if now > datetime.fromisoformat(end_str):
                        ok_time = False
                except Exception:
                    pass
            if not ok_time:
                continue
            active_users.append(user)
        except Exception:
            continue

    if active_users:
        for user in active_users:
            tg_token = user.get("telegram_bot_token") or DEFAULT_TELEGRAM_BOT_TOKEN
            tg_chat = user.get("telegram_chat_id") or DEFAULT_TELEGRAM_CHAT_ID
            sc_key = user.get("serverchan_sendkey") or DEFAULT_SERVERCHAN_SENDKEY
            dt_hook = user.get("dingtalk_webhook") or DEFAULT_DINGTALK_WEBHOOK
            
            # Telegram 无限制，直接发送
            if tg_token and tg_chat:
                if send_telegram(text, tg_token, tg_chat):
                    sent_channels.append("Telegram")
                    success = True
            
            # Server酱 需要检查额度（每个用户独立检查）
            if sc_key:
                if can_send_serverchan():
                    if send_serverchan(text, sc_key):
                        sent_channels.append("Server酱")
                        success = True
                else:
                    logger.debug(f"Server酱今日额度已用完（{MAX_DAILY_SENDS}条），跳过发送给用户 {user.get('name', '未知')}")
            
            # 钉钉 无限制，直接发送
            if dt_hook:
                if send_dingtalk(text, dt_hook):
                    sent_channels.append("钉钉")
                    success = True
        
        add_send_log(msg_type, text, sent_channels, success)
        return success

    # 如无有效用户，则不发送
    logger.warning("没有有效用户，跳过发送")
    return False


# ========== CLI 监控：脱锚 + 跨链套利告警 ==========

def run_cli_monitor_with_alerts():
    """
    命令行模式：循环监控 + Telegram 告警（如果配置了）。
    - 单个稳定币是否脱锚的告警
    - 同一稳定币在多链之间的跨链套利机会告警（已扣除成本）
    """
    logger.info("=" * 60)
    logger.info("多链稳定币脱锚 & 跨链套利监控（CLI 模式）启动")
    logger.info(f"启动时间（北京时间）: {format_beijing()}")
    logger.info("建议在后台长期运行，配合 Telegram 告警使用")
    logger.info("按 Ctrl + C 退出")
    logger.info("=" * 60)

    # 记录每个 (name, chain) 是否处于脱锚状态
    last_alert_state: dict[str, bool] = {}
    # 记录上一次已经推送过的套利机会，避免刷屏
    last_arb_alerts: dict[str, float] = {}  # key -> 上次推送时间戳
    # 心跳：最近一次发送时间 & 统计数据
    last_heartbeat_ts: float = 0.0
    total_alerts: int = 0
    total_arb_opps: int = 0

    # 初次加载配置（后续每轮循环会重新从文件读取一次，支持热更新）
    stable_configs = load_stable_configs()
    if not stable_configs:
        logger.warning("未设置任何稳定币监控配置，请先通过 Streamlit 面板添加后再运行 CLI")
        return

    while True:
        loop_start = time.time()
        try:
            # 每轮从文件加载一次配置，方便你在面板或手工改 JSON 后，CLI 自动生效
            stable_configs = load_stable_configs()
            if not stable_configs:
                logger.warning("当前没有任何监控配置，等待添加配置")
                time.sleep(DEFAULT_CHECK_INTERVAL)
                continue

            statuses = fetch_all_stable_status(
                stable_configs, global_threshold=DEFAULT_THRESHOLD
            )
            if not statuses:
                logger.warning("当前未获取到任何稳定币数据，请检查配置或网络")
                time.sleep(DEFAULT_CHECK_INTERVAL)
                continue

            logger.info("-" * 80)
            logger.info(f"检查时间: {format_beijing()}")
            logger.info("当前稳定币价格与脱锚情况：")

            for s in statuses:
                name = s["name"]
                chain = s["chain"]
                price = s["price"]
                dev = s["deviation_pct"]
                threshold = s["threshold"]
                is_alert = s["is_alert"]
                symbol = (s.get("symbol") or "").upper()

                status_msg = (
                    f"{name:15s} | 链: {chain:10s} | 价格: {price:.6f} USD | "
                    f"偏离: {dev:+.3f}% | 阈值: ±{threshold:.3f}% | "
                    f"{'⚠️脱锚' if is_alert else '✅正常'}"
                )
                
                if is_alert:
                    logger.warning(status_msg)
                else:
                    logger.info(status_msg)

                # 单币脱锚 Telegram 提醒（只在"刚从正常变为脱锚"时发一次）
                key_nc = f"{name}_{chain}"
                prev = last_alert_state.get(key_nc, False)
                if is_alert and not prev:
                    # 注意：Telegram 和钉钉无限制，Server酱 有5条限制，但 send_all_notifications 会自动处理
                    # 使用 Coingecko 做一次全局 cross-check + 稳定币对交叉核对
                    global_text = ""
                    if symbol:
                        cg_prices = get_coingecko_prices([symbol])
                        cg_price = cg_prices.get(symbol)
                        if cg_price:
                            global_dev = (cg_price - 1.0) * 100
                            global_text = (
                                f"\nCoingecko 全局参考: {symbol} ≈ {cg_price:.6f} USD "
                                f"(全局偏离 {global_dev:+.3f}%)."
                            )

                    pair_text = build_pair_crosscheck_text(s)

                    msg = (
                        f"[稳定币脱锚告警]\n"
                        f"{name} ({chain})\n"
                        f"价格: {price:.6f} USD\n"
                        f"偏离: {dev:+.3f}% (阈值 ±{threshold:.3f}%)"
                        f"{global_text}{pair_text}"
                    )
                    send_all_notifications(msg, msg_type="脱锚告警")
                    total_alerts += 1
                last_alert_state[key_nc] = is_alert

            # ========= 跨链套利机会扫描（使用优化参数）=========
            opps = find_arbitrage_opportunities(
                statuses,
                min_profit_usd=MIN_PROFIT_USD,
                min_profit_rate=MIN_PROFIT_RATE,
                min_spread_pct=MIN_PRICE_DIFF_PCT,
            )
            if opps:
                logger.info(f"\n🎯 检测到 {len(opps)} 个潜在跨链套利机会（已按默认成本参数估算）：")
                for opp in opps:
                    cd = opp["cost_detail"]
                    name = opp["name"]
                    cheap_chain = opp["cheap_chain"]
                    rich_chain = opp["rich_chain"]

                    opp_msg = (
                        f"💰 {name}: {cheap_chain} -> {rich_chain} | "
                        f"买价: {opp['cheap_price']:.6f} | 卖价: {opp['rich_price']:.6f} | "
                        f"价差: {cd['价差百分比']:+.3f}% | "
                        f"预估净利润: ${cd['预估净利润']:.2f} "
                        f"({cd['预估净利润率']:+.3f}%)"
                        + (
                            f" | 盈亏平衡资金: ${cd['盈亏平衡资金规模']:.2f}"
                            if cd.get("盈亏平衡资金规模") not in (None, 0)
                            else ""
                        )
                    )
                    logger.info(opp_msg)

                    # Telegram 套利机会提醒（对同一机会做时间防抖）
                    key = f"{name}:{cheap_chain}->{rich_chain}"
                    now_ts = time.time()
                    last_ts = last_arb_alerts.get(key, 0.0)
                    # 同一机会 5 分钟内只推一次
                    if now_ts - last_ts > 300:
                        # 注意：Telegram 和钉钉无限制，Server酱 有5条限制，但 send_all_notifications 会自动处理
                        msg = (
                            "[跨链套利机会]\n"
                            f"{name}\n"
                            f"买入链: {cheap_chain}  价格: {opp['cheap_price']:.6f} USD\n"
                            f"卖出链: {rich_chain}  价格: {opp['rich_price']:.6f} USD\n"
                            f"理论价差: {cd['价差百分比']:+.3f}%\n"
                            f"按资金规模 ${DEFAULT_TRADE_AMOUNT_USD:.0f} 估算：\n"
                            f"预估净利润: ${cd['预估净利润']:.2f} "
                            f"(净利率 {cd['预估净利润率']:+.3f}%)\n"
                            f"成本明细: 源链Gas ${cd['Gas费（源链）']:.2f} / "
                            f"目标链Gas ${cd['Gas费（目标链）']:.2f} / "
                            f"跨链桥费 ${cd['跨链桥费']:.2f} / 滑点损失 ${cd['滑点损失']:.2f}"
                        )
                        send_all_notifications(msg, msg_type="套利机会")
                        total_arb_opps += 1
                        last_arb_alerts[key] = now_ts
            else:
                logger.info("\n当前未发现达到阈值的跨链套利机会")

            # ========= 心跳通知（每天 12:00 固定时间） =========
            if should_send_heartbeat():
                # 注意：Telegram 和钉钉无限制，Server酱 有5条限制，但 send_all_notifications 会自动处理
                logger.info("⏰ 到达固定心跳时间 (12:00)，发送心跳通知...")
                hb_time = format_beijing()
                serverchan_count = get_today_send_count("Server酱")
                serverchan_remaining = MAX_DAILY_SENDS - serverchan_count
                    
                    # 统计链的数量
                    unique_chains = set(s.get("chain", "") for s in statuses if s.get("chain"))
                    chain_count = len(unique_chains)
                    
                    # 统计稳定币的数量（按 symbol，如果没有则按 name）
                    unique_symbols = set()
                    for s in statuses:
                        symbol = (s.get("symbol") or s.get("name") or "").upper()
                        if symbol:
                            unique_symbols.add(symbol)
                    symbol_count = len(unique_symbols)
                    
                    # 生成监控清单（按稳定币分组，显示各链的价格）
                    monitor_list = []
                    from collections import defaultdict
                    by_symbol = defaultdict(list)
                    for s in statuses:
                        symbol = (s.get("symbol") or s.get("name") or "").upper()
                        if symbol:
                            by_symbol[symbol].append(s)
                    
                    # 按稳定币名称排序
                    for symbol in sorted(by_symbol.keys()):
                        chains_info = []
                        for s in sorted(by_symbol[symbol], key=lambda x: x.get("chain", "")):
                            chain = s.get("chain", "未知")
                            price = s.get("price", 0)
                            dev = s.get("deviation_pct", 0)
                            is_alert = s.get("is_alert", False)
                            status_icon = "⚠️" if is_alert else "✅"
                            chains_info.append(f"{chain}: ${price:.4f} ({dev:+.2f}%){status_icon}")
                        if chains_info:
                            # 如果链数量较多，换行显示；否则用逗号连接
                            if len(chains_info) > 3:
                                chains_text = "\n    " + ", ".join(chains_info)
                            else:
                                chains_text = " " + ", ".join(chains_info)
                            monitor_list.append(f"  • {symbol}:{chains_text}")
                    
                    # 构建心跳消息
                    hb_msg = (
                        "[脱锚监控心跳 - 每日定时]\n"
                        f"⏰ 时间: {hb_time}\n"
                        f"📊 监控统计:\n"
                        f"  - 监控池数量: {len(statuses)} 个\n"
                        f"  - 检测链数量: {chain_count} 条\n"
                        f"  - 稳定币种类: {symbol_count} 种\n"
                        f"⚠️ 本次循环检测到的脱锚数量: "
                        f"{sum(1 for s in statuses if s['is_alert'])}\n"
                        f"📈 累计脱锚告警次数: {total_alerts}\n"
                        f"💰 累计跨链套利机会通知次数: {total_arb_opps}\n"
                        f"📤 Server酱额度: {serverchan_count}/{MAX_DAILY_SENDS} 条，剩余: {serverchan_remaining} 条\n"
                        f"💡 提示: Telegram 和钉钉无限制，可随时发送\n"
                        f"\n📋 监控清单:\n"
                    )
                    
                    # 添加清单（如果清单太长，只显示前20个，避免消息过长）
                    if monitor_list:
                        if len(monitor_list) > 20:
                            hb_msg += "\n".join(monitor_list[:20])
                            hb_msg += f"\n  ... 还有 {len(monitor_list) - 20} 个监控项（已省略）"
                        else:
                            hb_msg += "\n".join(monitor_list)
                    else:
                        hb_msg += "  （暂无监控项）"
                    
                send_all_notifications(hb_msg, msg_type="心跳")
                logger.info("✅ 心跳发送成功（Telegram 和钉钉已发送，Server酱 根据额度自动处理）")

            # ========= 控制循环频率 =========
            elapsed = time.time() - loop_start
            sleep_sec = max(1, DEFAULT_CHECK_INTERVAL - elapsed)
            time.sleep(sleep_sec)

        except KeyboardInterrupt:
            logger.info("\n用户手动停止监控")
            break
        except Exception as e:
            logger.error(f"主循环错误: {e}", exc_info=True)
            time.sleep(DEFAULT_CHECK_INTERVAL)


# ========== Streamlit 面板（前端表现层） ==========

def run_streamlit_panel():
    st.set_page_config(
        page_title="多链稳定币脱锚监控",
        page_icon="🎯",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    # 简洁的 CSS 样式
    st.markdown("""
    <style>
        /* 按钮美化 */
        .stButton button {
            border-radius: 5px;
            font-size: 14px;
        }
        
        /* 表格美化 */
        .dataframe {
            font-size: 14px;
        }
    </style>
    """, unsafe_allow_html=True)
    
    # ----- 登录检查 -----
    if not check_login():
        st.stop()  # 未登录则停止执行
    
    # 显示登录信息和退出按钮
    with st.sidebar:
        st.markdown("---")
        if st.session_state.get("username"):
            st.info(f"👤 已登录: {st.session_state['username']}")
            
            # 修改密码功能
            with st.expander("🔐 修改密码"):
                new_password = st.text_input("新密码", type="password", key="new_password_input")
                confirm_password = st.text_input("确认新密码", type="password", key="confirm_password_input")
                if st.button("保存新密码", key="save_password_btn"):
                    if not new_password:
                        st.warning("密码不能为空")
                    elif new_password != confirm_password:
                        st.error("两次输入的密码不一致")
                    else:
                        try:
                            config = load_auth_config()
                            # 生成新密码哈希（使用安全的 PBKDF2）
                            new_password_hash, new_salt = hash_password_secure(new_password)
                            config["password_hash"] = new_password_hash
                            config["salt"] = new_salt
                            save_auth_config(config)
                            logger.info(f"用户 {st.session_state.get('username')} 修改了密码")
                            st.success("密码已修改，请重新登录")
                            # 清除登录状态
                            st.session_state["authentication_status"] = False
                            if "username" in st.session_state:
                                del st.session_state["username"]
                            st.rerun()
                        except Exception as e:
                            st.error(f"修改密码失败: {e}")
            
            if st.button("🚪 退出登录"):
                # 清除登录状态
                st.session_state["authentication_status"] = False
                if "username" in st.session_state:
                    del st.session_state["username"]
                st.rerun()

    st.title("🎯 多链稳定币脱锚监控面板")

    # ----- 初始化 Session State -----
    if "check_interval" not in st.session_state:
        st.session_state["check_interval"] = DEFAULT_CHECK_INTERVAL
    
    # 每次页面加载时都重新从文件加载配置，确保显示最新数据
    # 这样添加配置后能立即看到效果
    st.session_state["stable_configs"] = load_stable_configs()

    # 用户配置（多用户通知分发）
    if "users" not in st.session_state:
        st.session_state["users"] = load_users()

    # 面板专用的套利参数（不影响 CLI 默认参数）
    if "arb_trade_amount" not in st.session_state:
        st.session_state["arb_trade_amount"] = DEFAULT_TRADE_AMOUNT_USD
    if "arb_src_gas" not in st.session_state:
        st.session_state["arb_src_gas"] = DEFAULT_SRC_GAS_USD
    if "arb_dst_gas" not in st.session_state:
        st.session_state["arb_dst_gas"] = DEFAULT_DST_GAS_USD
    if "arb_bridge_fee" not in st.session_state:
        st.session_state["arb_bridge_fee"] = DEFAULT_BRIDGE_FEE_USD
    if "arb_slippage_pct" not in st.session_state:
        st.session_state["arb_slippage_pct"] = DEFAULT_SLIPPAGE_PCT
    if "arb_min_profit_usd" not in st.session_state:
        st.session_state["arb_min_profit_usd"] = DEFAULT_MIN_PROFIT_USD
    if "arb_min_profit_rate" not in st.session_state:
        st.session_state["arb_min_profit_rate"] = DEFAULT_MIN_PROFIT_RATE
    if "arb_min_spread_pct" not in st.session_state:
        st.session_state["arb_min_spread_pct"] = DEFAULT_MIN_SPREAD_PCT
    if "last_alert_state" not in st.session_state:
        st.session_state["last_alert_state"] = {}
    if "history" not in st.session_state:
        # DataFrame: timestamp, name(交易对名), symbol(稳定币符号), chain, price, deviation_pct
        st.session_state["history"] = pd.DataFrame(
            columns=["timestamp", "name", "symbol", "chain", "price", "deviation_pct"]
        )

    # 全局配置（LI.FI API Key 等）
    if "lifi_api_key" not in st.session_state:
        gcfg = load_global_config()
        st.session_state["lifi_api_key"] = gcfg.get("lifi_api_key", "")
        st.session_state["lifi_from_address"] = gcfg.get("lifi_from_address", "")
    
    # UI 配置持久化（价格曲线选择、脱锚阈值等）
    if "ui_config" not in st.session_state:
        gcfg = load_global_config()
        ui_config = gcfg.get("ui_config", {})
        st.session_state["ui_config"] = ui_config
        st.session_state["selected_symbols"] = ui_config.get("selected_symbols", [])
        st.session_state["saved_global_threshold"] = ui_config.get("global_threshold", DEFAULT_THRESHOLD)
        st.session_state["global_threshold"] = st.session_state["saved_global_threshold"]

    # ----- 侧边栏：全局配置 & 稳定币配置 -----
    with st.sidebar:
        st.subheader("全局设置")
        st.session_state["lifi_api_key"] = st.text_input(
            "LI.FI API Key（可选，用于更高精度/更高频率的跨链 & 同链报价）",
            value=st.session_state["lifi_api_key"],
            type="password",
        )
        lifi_address_input = st.text_input(
            "LI.FI fromAddress（你的 EVM 钱包地址，仅用于报价，不做交易）",
            value=st.session_state.get("lifi_from_address", ""),
            help="格式: 0x 开头的 40 位十六进制字符"
        )
        
        # 验证地址格式
        if lifi_address_input and not is_valid_ethereum_address(lifi_address_input):
            st.warning("⚠️ 地址格式不正确，应为 0x 开头的 42 位十六进制地址")
        
        st.session_state["lifi_from_address"] = lifi_address_input
        st.session_state["check_interval"] = st.number_input(
            "刷新间隔（秒）",
            min_value=5,
            max_value=120,
            value=int(st.session_state["check_interval"]),
            step=1,
        )
        auto_refresh = st.checkbox(
            "页面自动刷新（按以上间隔）",
            value=st.session_state.get("auto_refresh", False),
        )
        st.session_state["auto_refresh"] = auto_refresh
        default_anchor = st.number_input(
            "默认锚定价（一般稳定币为 1.0）",
            min_value=0.1,
            max_value=10.0,
            value=float(DEFAULT_ANCHOR_PRICE),
            step=0.01,
        )
        default_threshold = st.number_input(
            "默认脱锚阈值（%）",
            min_value=0.1,
            max_value=50.0,
            value=float(st.session_state.get("saved_global_threshold", DEFAULT_THRESHOLD)),
            step=0.1,
            key="global_threshold_input",
        )
        st.session_state["global_threshold"] = default_threshold
        
        # 当阈值改变时自动保存
        if st.session_state.get("saved_global_threshold") != default_threshold:
            st.session_state["saved_global_threshold"] = default_threshold
            # 自动保存到配置文件
            gcfg = load_global_config()
            if "ui_config" not in gcfg:
                gcfg["ui_config"] = {}
            gcfg["ui_config"]["global_threshold"] = default_threshold
            save_global_config(gcfg)
        
        st.markdown("---")
        st.markdown("### 💰 套利优化配置")
        
        min_profit_usd = st.number_input(
            "最小净利润（USD）",
            min_value=1.0,
            max_value=1000.0,
            value=float(MIN_PROFIT_USD),
            step=10.0,
            help="过滤低于此金额的套利机会"
        )
        
        min_profit_rate = st.number_input(
            "最小净利率（%）",
            min_value=0.1,
            max_value=50.0,
            value=float(MIN_PROFIT_RATE),
            step=0.5,
            help="过滤低于此利率的套利机会"
        )
        
        min_price_diff = st.number_input(
            "最小价差（%）",
            min_value=0.1,
            max_value=10.0,
            value=float(MIN_PRICE_DIFF_PCT),
            step=0.1,
            help="链间价差低于此值将被忽略"
        )
        
        st.caption(f"⚡ 监控间隔: {DEFAULT_CHECK_INTERVAL}秒")
        st.caption(f"🔄 并发请求数: {MAX_CONCURRENT_REQUESTS}")
        st.caption(f"📊 缓存策略: 价格{CACHE_TTL_PRICE}s / Gas{CACHE_TTL_GAS}s")

        # 保存全局配置按钮（包括 LI.FI API Key / fromAddress / UI 配置）
        col_save, col_clear = st.columns(2)
        with col_save:
            if st.button("💾 保存全局配置", use_container_width=True):
                gcfg = {
                    "lifi_api_key": st.session_state.get("lifi_api_key", ""),
                    "lifi_from_address": st.session_state.get("lifi_from_address", ""),
                    "ui_config": {
                        "global_threshold": st.session_state.get("global_threshold", DEFAULT_THRESHOLD),
                        "selected_symbols": st.session_state.get("selected_symbols", []),
                    }
                }
                save_global_config(gcfg)
                st.session_state["saved_global_threshold"] = gcfg["ui_config"]["global_threshold"]
                st.success(f"全局配置已保存到 {GLOBAL_CONFIG_FILE}。")
        
        with col_clear:
            if st.button("🗑️ 清除缓存", use_container_width=True, help="清除 API 缓存，强制重新获取数据"):
                _global_cache.clear()
                st.success("缓存已清除")
                logger.info("用户手动清除了缓存")
                st.rerun()

        st.markdown("---")
        st.subheader("跨链套利参数（面板展示用）")
        st.session_state["arb_trade_amount"] = st.number_input(
            "默认套利资金规模（USD）",
            min_value=10.0,
            max_value=1_000_000.0,
            value=float(st.session_state["arb_trade_amount"]),
            step=10.0,
        )
        col_g1, col_g2 = st.columns(2)
        st.session_state["arb_src_gas"] = col_g1.number_input(
            "源链默认 Gas（USD）",
            min_value=0.0,
            max_value=100.0,
            value=float(st.session_state["arb_src_gas"]),
            step=0.1,
        )
        st.session_state["arb_dst_gas"] = col_g2.number_input(
            "目标链默认 Gas（USD）",
            min_value=0.0,
            max_value=100.0,
            value=float(st.session_state["arb_dst_gas"]),
            step=0.1,
        )
        col_b1, col_b2 = st.columns(2)
        st.session_state["arb_bridge_fee"] = col_b1.number_input(
            "默认跨链桥费用（USD）",
            min_value=0.0,
            max_value=100.0,
            value=float(st.session_state["arb_bridge_fee"]),
            step=0.5,
        )
        st.session_state["arb_slippage_pct"] = col_b2.number_input(
            "默认往返滑点总和（%）",
            min_value=0.0,
            max_value=20.0,
            value=float(st.session_state["arb_slippage_pct"]),
            step=0.1,
        )
        col_p1, col_p2, col_p3 = st.columns(3)
        st.session_state["arb_min_spread_pct"] = col_p1.number_input(
            "最小价差（%）",
            min_value=0.0,
            max_value=10.0,
            value=float(st.session_state["arb_min_spread_pct"]),
            step=0.05,
        )
        st.session_state["arb_min_profit_usd"] = col_p2.number_input(
            "最小净利润（USD）",
            min_value=0.0,
            max_value=10_000.0,
            value=float(st.session_state["arb_min_profit_usd"]),
            step=1.0,
        )
        st.session_state["arb_min_profit_rate"] = col_p3.number_input(
            "最小净利率（%）",
            min_value=0.0,
            max_value=10.0,
            value=float(st.session_state["arb_min_profit_rate"]),
            step=0.01,
        )

        st.markdown("---")
        st.subheader("用户管理（多用户通知分发）")

        users: list[dict] = st.session_state["users"]
        user_options = ["<新建用户>"] + [
            f"{u.get('name', '未命名')} ({u.get('id','')})" for u in users
        ]
        selected_user = st.selectbox("选择用户", options=user_options, key="user_select")

        current_user: dict
        if selected_user != "<新建用户>":
            # 从括号中提取 id
            sel_id = selected_user.split("(")[-1].rstrip(")")
            current_user = next((u for u in users if u.get("id") == sel_id), {})
        else:
            current_user = {}

        user_name = st.text_input(
            "用户名称（仅标记用）", value=current_user.get("name", "")
        )
        u_tg_token = st.text_input(
            "Telegram Bot Token",
            value=current_user.get("telegram_bot_token", ""),
            type="password",
        )
        u_tg_chat = st.text_input(
            "Telegram Chat ID",
            value=current_user.get("telegram_chat_id", ""),
        )
        u_sc_key = st.text_input(
            "Server酱 SendKey",
            value=current_user.get("serverchan_sendkey", ""),
        )
        u_dt_hook = st.text_input(
            "钉钉 Webhook",
            value=current_user.get("dingtalk_webhook", ""),
        )
        # 订阅起止时间使用日期选择器，精度到天（内部仍保存为 ISO 字符串）
        today = now_beijing().date()
        try:
            parsed_start = (
                datetime.fromisoformat(current_user.get("start_at"))
                .date()
                if current_user.get("start_at")
                else today
            )
        except Exception:
            parsed_start = today
        try:
            parsed_end = (
                datetime.fromisoformat(current_user.get("end_at"))
                .date()
                if current_user.get("end_at")
                else today
            )
        except Exception:
            parsed_end = today

        u_start_date = st.date_input("订阅开始日期", value=parsed_start)
        u_end_date = st.date_input("订阅结束日期", value=parsed_end)
        u_enabled = st.checkbox(
            "启用该用户", value=current_user.get("enabled", True)
        )

        col_ua, col_ub, col_uc = st.columns(3)
        with col_ua:
            if st.button("保存/更新用户"):
                if not user_name:
                    st.warning("用户名称不能为空。")
                else:
                    if current_user.get("id"):
                        user_id = current_user["id"]
                    else:
                        user_id = f"user_{int(time.time())}"
                    updated_user = {
                        "id": user_id,
                        "name": user_name,
                        "telegram_bot_token": u_tg_token,
                        "telegram_chat_id": u_tg_chat,
                        "serverchan_sendkey": u_sc_key,
                        "dingtalk_webhook": u_dt_hook,
                        # 存储为 ISO 字符串，时间统一设为 00:00:00
                        "start_at": datetime.combine(
                            u_start_date, datetime.min.time()
                        ).isoformat(),
                        "end_at": datetime.combine(
                            u_end_date, datetime.min.time()
                        ).isoformat(),
                        "enabled": u_enabled,
                    }
                    found = False
                    for idx, u in enumerate(users):
                        if u.get("id") == user_id:
                            users[idx] = updated_user
                            found = True
                            break
                    if not found:
                        users.append(updated_user)
                    st.session_state["users"] = users
                    save_users(users)
                    st.success(f"用户已保存：{user_name}")
        with col_ub:
            if (
                st.button("删除当前用户")
                and selected_user != "<新建用户>"
                and current_user.get("id")
            ):
                users = [u for u in users if u.get("id") != current_user["id"]]
                st.session_state["users"] = users
                save_users(users)
                st.success(f"已删除用户：{current_user.get('name','')}")
        with col_uc:
            if (
                st.button("测试当前用户通知")
                and current_user.get("id")
            ):
                hb_time = format_beijing()
                test_msg = (
                    "[手工测试通知]\n"
                    f"时间: {hb_time}\n"
                    f"用户: {current_user.get('name','')}\n"
                    "这是一条从面板按钮触发的测试消息，用于验证该用户的通知渠道是否正常。"
                )
                test_cfg = {
                    "telegram_bot_token": u_tg_token,
                    "telegram_chat_id": u_tg_chat,
                    "serverchan_sendkey": u_sc_key,
                    "dingtalk_webhook": u_dt_hook,
                }
                send_all_notifications(test_msg, test_cfg)
                st.success("已向该用户配置的渠道发送测试通知。")

        st.markdown("---")
        st.subheader("监控的稳定币配置")
        
        # ========== 自动采集稳定币对功能 ==========
        st.markdown("#### 🤖 自动采集稳定币对")
        st.caption("使用 DexScreener API 自动搜索并添加稳定币交易对")
        
        # 初始化 session state（从文件加载采集结果，实现持久化）
        if "collected_pairs_cache" not in st.session_state:
            # 从文件加载之前保存的采集结果
            cached_pairs = load_collected_pairs_cache()
            st.session_state["collected_pairs_cache"] = cached_pairs
            if cached_pairs:
                logger.info(f"已从文件恢复 {len(cached_pairs)} 个采集结果")
        if "available_chains" not in st.session_state:
            # 第一次初始化时，直接使用已知的链列表（不为空）
            st.session_state["available_chains"] = list(CHAIN_NAME_TO_ID.keys())
        
        # 从 API 获取支持的链列表（可选，用于获取最新的链）
        with st.expander("⚙️ 链列表管理", expanded=False):
            col_refresh, col_info = st.columns([1, 2])
            with col_refresh:
                if st.button("🔄 刷新链列表", use_container_width=True, help="从 DexScreener API 获取最新支持的链列表"):
                    with st.spinner("正在从 API 获取支持的链列表..."):
                        try:
                            chains = get_available_chains_from_api()
                            st.session_state["available_chains"] = chains
                            st.success(f"✅ 已获取 {len(chains)} 条链")
                            st.rerun()
                        except Exception as e:
                            st.error(f"❌ 获取链列表失败: {e}")
            with col_info:
                st.caption(f"当前可用链数: **{len(st.session_state['available_chains'])}** 条")
        
        # 获取所有稳定币符号（包括自定义）
        all_stable_symbols = get_all_stable_symbols()
        
        # 自定义稳定币管理
        with st.expander("➕ 自定义稳定币管理", expanded=False):
            new_symbol = st.text_input(
                "稳定币符号（如：USD0, FRAX 等）",
                value="",
                key="new_custom_symbol",
                help="输入稳定币符号，会自动转换为大写",
            )
            col_add1, col_add2, col_add3 = st.columns([1, 1, 1])
            with col_add1:
                if st.button("➕ 添加", key="add_custom_symbol", use_container_width=True):
                    if new_symbol:
                        symbol_upper = new_symbol.upper().strip()
                        if symbol_upper:
                            custom_symbols = load_custom_stable_symbols()
                            if symbol_upper not in custom_symbols:
                                custom_symbols.append(symbol_upper)
                                save_custom_stable_symbols(custom_symbols)
                                st.success(f"✅ 已添加: {symbol_upper}")
                                st.rerun()
                            else:
                                st.warning(f"⚠️ {symbol_upper} 已存在")
                        else:
                            st.warning("⚠️ 请输入有效的稳定币符号")
            with col_add2:
                if st.button("📋 查看列表", key="view_custom_symbols", use_container_width=True):
                    custom_symbols = load_custom_stable_symbols()
                    if custom_symbols:
                        st.info("已添加的自定义稳定币: " + ", ".join(custom_symbols))
                    else:
                        st.info("暂无自定义稳定币")
            with col_add3:
                # 显示自定义稳定币数量
                custom_count = len(load_custom_stable_symbols())
                st.caption(f"自定义数量: **{custom_count}**")
        
        # 初始化选择状态（优化：统一使用 widget key 作为状态变量，避免冲突）
        if "auto_symbols_multiselect" not in st.session_state:
            # 默认选择主流稳定币（不超过5个，避免侧边栏过长）
            default_symbols = ["USDT", "USDC", "DAI"]
            st.session_state["auto_symbols_multiselect"] = [s for s in default_symbols if s in all_stable_symbols]
        
        if "auto_chains_multiselect" not in st.session_state:
            # 默认选择主流链（不超过8个）
            main_chains = ["ethereum", "bsc", "polygon", "arbitrum", "optimism", "base", "avalanche", "zksync"]
            available = st.session_state["available_chains"]
            st.session_state["auto_chains_multiselect"] = [c for c in main_chains if c in available][:8]
        
        # 稳定币选择器（优化布局和交互）
        st.markdown("**📊 选择要采集的稳定币**")
        col_symbols_btn1, col_symbols_btn2, col_symbols_btn3 = st.columns([1, 1, 2])
        with col_symbols_btn1:
            if st.button("✅ 全选稳定币", key="select_all_symbols", use_container_width=True):
                st.session_state["auto_symbols_multiselect"] = list(all_stable_symbols)
                st.rerun()
        with col_symbols_btn2:
            if st.button("❌ 清空稳定币", key="clear_all_symbols", use_container_width=True):
                st.session_state["auto_symbols_multiselect"] = []
                st.rerun()
        with col_symbols_btn3:
            selected_count = len(st.session_state.get("auto_symbols_multiselect", []))
            st.caption(f"已选择: **{selected_count}** / {len(all_stable_symbols)} 个稳定币")
        
        # 注意：使用 key 时，不要同时使用 default 参数，widget 会自动从 session_state[key] 读取值
        auto_symbols = st.multiselect(
            "稳定币（可多选，支持搜索）",
            options=all_stable_symbols,
            help="💡 在输入框中输入关键词可快速搜索稳定币",
            key="auto_symbols_multiselect",
        )
        
        # 链选择器（优化布局和交互）
        st.markdown("**⛓️ 选择要搜索的链**")
        col_chains_btn1, col_chains_btn2, col_chains_btn3 = st.columns([1, 1, 2])
        with col_chains_btn1:
            if st.button("✅ 全选链", key="select_all_chains", use_container_width=True):
                st.session_state["auto_chains_multiselect"] = list(st.session_state["available_chains"])
                st.rerun()
        with col_chains_btn2:
            if st.button("❌ 清空链", key="clear_all_chains", use_container_width=True):
                st.session_state["auto_chains_multiselect"] = []
                st.rerun()
        with col_chains_btn3:
            selected_chains_count = len(st.session_state.get("auto_chains_multiselect", []))
            st.caption(f"已选择: **{selected_chains_count}** / {len(st.session_state['available_chains'])} 条链")
        
        # 注意：使用 key 时，不要同时使用 default 参数，widget 会自动从 session_state[key] 读取值
        auto_chains = st.multiselect(
            "链（可多选，支持搜索）",
            options=st.session_state["available_chains"],
            help="💡 在输入框中输入关键词可快速搜索链名",
            key="auto_chains_multiselect",
        )
        
        # 最小流动性（默认 50 万美金，更合理）
        auto_min_liq = st.number_input(
            "💰 最小流动性（USD）",
            min_value=0.0,
            max_value=10_000_000.0,
            value=500_000.0,  # 默认 50 万美金（降低门槛）
            step=10000.0,
            help="💡 只添加流动性大于此值的交易对（建议: 50万-100万 USD）",
        )
        
        if st.button("🚀 开始自动采集", type="primary", use_container_width=True):
            if not auto_symbols:
                st.warning("请至少选择一个稳定币符号")
            elif not auto_chains:
                st.warning("请至少选择一条链")
            else:
                # 显示实际使用的参数（调试用）
                st.info(f"📊 将在 **{len(auto_chains)}** 条链上搜索 **{len(auto_symbols)}** 个稳定币")
                with st.expander("🔍 查看详细参数"):
                    st.write(f"**稳定币列表** ({len(auto_symbols)} 个):")
                    st.write(", ".join(auto_symbols))
                    st.write(f"**链列表** ({len(auto_chains)} 条):")
                    st.write(", ".join(auto_chains))
                    st.write(f"**最小流动性**: ${auto_min_liq:,.0f}")
                
                # 创建进度条和状态容器
                progress_bar = st.progress(0)
                status_text = st.empty()
                
                try:
                    # 进度回调函数
                    def update_progress(current: int, total: int, message: str):
                        progress = current / total
                        progress_bar.progress(progress)
                        status_text.text(f"📊 {message} ({current}/{total})")
                    
                    # 执行采集（带速率限制）
                    collected_pairs, stats = auto_collect_stablecoin_pairs(
                        stable_symbols=auto_symbols,
                        chains=auto_chains,
                        min_liquidity_usd=float(auto_min_liq),
                        max_results_per_symbol=10,
                        progress_callback=update_progress,
                    )
                    
                    # 完成进度条
                    progress_bar.progress(1.0)
                    
                    # 保存到 session state 和文件（实现持久化）
                    st.session_state["collected_pairs_cache"] = collected_pairs
                    st.session_state["collection_stats"] = stats
                    # 持久化保存到文件
                    save_collected_pairs_cache(collected_pairs)
                    
                    # 显示统计信息
                    if not collected_pairs:
                        status_text.warning("❌ 未找到符合条件的交易对，请尝试降低流动性要求或选择其他链")
                    else:
                        status_text.success(
                            f"✅ 采集完成！找到 **{stats['unique_pairs']}** 个符合条件的交易对\n"
                            f"📊 统计: 搜索 {stats['total_symbols']} 个稳定币，"
                            f"共找到 {stats['total_pairs_found']} 个交易对（去重后 {stats['unique_pairs']} 个），"
                            f"错误 {stats['errors']} 个，限流 {stats['rate_limit_stats']['rate_limited_count']} 次"
                        )
                        
                        # 显示速率限制统计
                        if stats['rate_limit_stats']['rate_limited_count'] > 0:
                            st.warning(
                                f"⚠️ 检测到 {stats['rate_limit_stats']['rate_limited_count']} 次 API 限流，"
                                f"已自动重试。建议减少并发或降低请求频率。"
                            )
                        
                        # 显示详细的速率限制信息
                        with st.expander("📈 查看详细统计信息", expanded=False):
                            st.json(stats)
                    
                    # 清除进度条
                    time.sleep(0.5)  # 短暂显示完成状态
                    progress_bar.empty()
                    
                except Exception as e:
                    progress_bar.empty()
                    status_text.error(f"❌ 自动采集失败: {e}")
                    import traceback
                    with st.expander("🔍 查看错误详情"):
                        st.code(traceback.format_exc())
        
        # 显示采集结果，支持多选勾选（优化：使用表格显示，性能更好）
        if st.session_state["collected_pairs_cache"]:
            collected_pairs = st.session_state["collected_pairs_cache"]
            
            st.markdown("---")
            st.markdown("### 📋 采集结果")
            
            # 初始化选中状态（使用列表而不是 set，便于保持顺序）
            if "selected_pair_indices" not in st.session_state:
                st.session_state["selected_pair_indices"] = []
            
            # 全选/全不选按钮（优化：减少不必要的 rerun）
            col_select_all, col_select_none, col_select_info, col_select_filter = st.columns([1, 1, 2, 1])
            with col_select_all:
                if st.button("✅ 全选", key="select_all_pairs", use_container_width=True):
                    # 只选择未存在且非危险的交易对
                    safe_indices = []
                    for idx, p in enumerate(collected_pairs):
                        exists = any(
                            cfg.get("chain") == p["chain"] 
                            and cfg.get("pair_address") == p["pair_address"]
                            for cfg in st.session_state["stable_configs"]
                        )
                        risk_level = p.get("legitimacy", {}).get("risk_level", "safe")
                        if not exists and risk_level != "danger":
                            safe_indices.append(idx)
                    st.session_state["selected_pair_indices"] = safe_indices
                    st.rerun()
            with col_select_none:
                if st.button("❌ 全不选", key="select_none_pairs", use_container_width=True):
                    st.session_state["selected_pair_indices"] = []
                    st.rerun()
            with col_select_info:
                selected_count = len(st.session_state["selected_pair_indices"])
                st.info(f"✅ 已选择: **{selected_count}** / {len(collected_pairs)} 个交易对")
            with col_select_filter:
                # 过滤选项
                filter_option = st.selectbox(
                    "筛选",
                    options=["全部", "仅安全", "仅存在", "仅危险"],
                    key="pair_filter",
                    label_visibility="collapsed",
                )
            
            # 根据筛选条件过滤交易对
            filtered_pairs = []
            for idx, p in enumerate(collected_pairs):
                exists = any(
                    cfg.get("chain") == p["chain"] 
                    and cfg.get("pair_address") == p["pair_address"]
                    for cfg in st.session_state["stable_configs"]
                )
                risk_level = p.get("legitimacy", {}).get("risk_level", "safe")
                
                if filter_option == "仅安全" and (exists or risk_level != "safe"):
                    continue
                elif filter_option == "仅存在" and not exists:
                    continue
                elif filter_option == "仅危险" and risk_level != "danger":
                    continue
                
                filtered_pairs.append((idx, p, exists, risk_level))
            
            if not filtered_pairs:
                st.warning("📭 没有符合条件的交易对")
            else:
                # 使用数据表格显示（性能更好，支持排序）
                display_data = []
                for idx, p, exists, risk_level in filtered_pairs:
                    base_sym = p["base_token"]["symbol"]
                    quote_sym = p["quote_token"]["symbol"]
                    pair_name = f"{base_sym}/{quote_sym}"
                    
                    # 风险标记
                    risk_icons = {"safe": "✅", "warning": "⚠️", "danger": "🚨"}
                    risk_icon = risk_icons.get(risk_level, "")
                    status = risk_icon + (" ⚠️已存在" if exists else "")
                    
                    is_selected = idx in st.session_state["selected_pair_indices"]
                    selectable = not exists and risk_level != "danger"
                    
                    display_data.append({
                        "选择": "✅" if is_selected else "⬜",
                        "状态": status,
                        "交易对": pair_name,
                        "链": p['chain'],
                        "流动性(USD)": f"${p['liquidity_usd']:,.0f}",
                        "价格(USD)": f"{p['price_usd']:.6f}" if p.get('price_usd') else "N/A",
                        "地址": p['pair_address'][:10] + "...",
                        "_idx": idx,
                        "_selectable": selectable,
                    })
                
                # 显示表格
                df_display = pd.DataFrame(display_data)
                st.dataframe(
                    df_display[["选择", "状态", "交易对", "链", "流动性(USD)", "价格(USD)"]],
                    use_container_width=True,
                    height=min(400, len(filtered_pairs) * 35 + 50),  # 自适应高度
                    hide_index=True,
                )
                
                # 使用复选框批量选择（优化：减少复选框数量，提升性能）
                st.markdown("**💡 快速选择（推荐）：**")
                col_batch1, col_batch2, col_batch3 = st.columns(3)
                
                with col_batch1:
                    if st.button("✅ 选择所有安全项", key="select_all_safe", use_container_width=True):
                        safe_indices = [idx for idx, _, exists, risk in filtered_pairs 
                                       if not exists and risk == "safe"]
                        current = set(st.session_state["selected_pair_indices"])
                        current.update(safe_indices)
                        st.session_state["selected_pair_indices"] = sorted(list(current))
                        st.rerun()
                
                with col_batch2:
                    if st.button("✅ 选择高流动性项（>100万）", key="select_high_liq", use_container_width=True):
                        high_liq_indices = [idx for idx, p, exists, risk in filtered_pairs 
                                           if not exists and risk != "danger" and p['liquidity_usd'] > 1_000_000]
                        current = set(st.session_state["selected_pair_indices"])
                        current.update(high_liq_indices)
                        st.session_state["selected_pair_indices"] = sorted(list(current))
                        st.rerun()
                
                with col_batch3:
                    if st.button("❌ 取消全部选择", key="clear_selected_pairs", use_container_width=True):
                        st.session_state["selected_pair_indices"] = []
                        st.rerun()
                
                # 如果需要，也可以展开显示详细复选框（可折叠，默认收起）
                with st.expander("🔽 展开详细选择（逐个勾选）", expanded=False):
                    # 限制显示数量，避免页面卡顿
                    max_display = 50
                    pairs_to_show = filtered_pairs[:max_display]
                    
                    if len(filtered_pairs) > max_display:
                        st.warning(f"⚠️ 仅显示前 {max_display} 个交易对（共 {len(filtered_pairs)} 个），请使用批量选择功能")
                    
                    for idx, p, exists, risk_level in pairs_to_show:
                        base_sym = p["base_token"]["symbol"]
                        quote_sym = p["quote_token"]["symbol"]
                        pair_name = f"{base_sym}/{quote_sym}"
                        
                        is_checked = idx in st.session_state["selected_pair_indices"]
                        selectable = not exists and risk_level != "danger"
                        
                        risk_icons = {"safe": "✅", "warning": "⚠️", "danger": "🚨"}
                        risk_icon = risk_icons.get(risk_level, "")
                        
                        col_cb, col_info = st.columns([0.3, 9.7])
                        with col_cb:
                            checkbox_key = f"pair_checkbox_detailed_{idx}"
                            new_checked = st.checkbox(
                                "",
                                value=is_checked,
                                key=checkbox_key,
                                disabled=not selectable,
                                label_visibility="collapsed",
                            )
                            # 更新选中状态
                            if new_checked and idx not in st.session_state["selected_pair_indices"]:
                                st.session_state["selected_pair_indices"].append(idx)
                            elif not new_checked and idx in st.session_state["selected_pair_indices"]:
                                st.session_state["selected_pair_indices"].remove(idx)
                        
                        with col_info:
                            exists_text = " ⚠️已存在" if exists else ""
                            disabled_text = " 🚨已禁用" if not selectable else ""
                            st.markdown(
                                f"{risk_icon} **{pair_name}**{exists_text}{disabled_text} | "
                                f"链: `{p['chain']}` | 流动性: `${p['liquidity_usd']:,.0f}` | "
                                f"价格: `{p['price_usd']:.6f}`" if p.get('price_usd') else f"价格: N/A"
                            )
            
            # 显示选中交易对的汇总（优化：更清晰的操作流程）
            selected_indices = st.session_state["selected_pair_indices"]
            if selected_indices:
                st.markdown("---")
                
                # 统计信息
                col_sum1, col_sum2, col_sum3 = st.columns(3)
                with col_sum1:
                    st.metric("已选择", f"{len(selected_indices)} 个")
                
                # 检查有多少会跳过（已存在）
                skipped_preview = 0
                for idx in selected_indices:
                    p = collected_pairs[idx]
                    exists = any(
                        cfg.get("chain") == p["chain"] 
                        and cfg.get("pair_address") == p["pair_address"]
                        for cfg in st.session_state["stable_configs"]
                    )
                    if exists:
                        skipped_preview += 1
                
                with col_sum2:
                    st.metric("可添加", f"{len(selected_indices) - skipped_preview} 个")
                with col_sum3:
                    st.metric("将跳过", f"{skipped_preview} 个")
                
                if skipped_preview > 0:
                    st.warning(f"⚠️ 其中有 {skipped_preview} 个交易对已存在于监控配置中，将被跳过")
                
                # 显示选中交易对的详细信息表格（可折叠）
                with st.expander(f"📋 查看已选择的 {len(selected_indices)} 个交易对详情", expanded=False):
                    selected_display = []
                    for idx in selected_indices:
                        p = collected_pairs[idx]
                        base_sym = p["base_token"]["symbol"]
                        quote_sym = p["quote_token"]["symbol"]
                        pair_name = f"{base_sym}/{quote_sym}"
                        
                        exists = any(
                            cfg.get("chain") == p["chain"] 
                            and cfg.get("pair_address") == p["pair_address"]
                            for cfg in st.session_state["stable_configs"]
                        )
                        
                        selected_display.append({
                            "交易对": pair_name,
                            "链": p["chain"],
                            "流动性(USD)": f"${p['liquidity_usd']:,.0f}",
                            "价格(USD)": f"{p['price_usd']:.6f}" if p.get('price_usd') else "N/A",
                            "状态": "⚠️已存在" if exists else "✅可添加",
                            "Pair地址": p["pair_address"],
                        })
                    
                    if selected_display:
                        st.dataframe(pd.DataFrame(selected_display), use_container_width=True, hide_index=True)
                
                # 添加到配置按钮（优化：更明确的反馈）
                col_btn1, col_btn2, col_btn3 = st.columns([2, 1, 1])
                with col_btn1:
                    if st.button("✅ 添加选中的交易对到监控配置", type="primary", use_container_width=True):
                        added_count = 0
                        skipped_count = 0
                        skipped_details = []
                        
                        for idx in selected_indices:
                            p = collected_pairs[idx]
                            base_sym = p["base_token"]["symbol"]
                            quote_sym = p["quote_token"]["symbol"]
                            pair_name = f"{base_sym}/{quote_sym}"
                            
                            # 检查是否已存在
                            exists = any(
                                cfg.get("chain") == p["chain"] 
                                and cfg.get("pair_address") == p["pair_address"]
                                for cfg in st.session_state["stable_configs"]
                            )
                            
                            if exists:
                                skipped_count += 1
                                skipped_details.append(f"{pair_name} ({p['chain']})")
                                continue
                            
                            new_cfg = {
                                "name": pair_name,
                                "chain": p["chain"],
                                "pair_address": p["pair_address"],
                                "anchor_price": default_anchor,
                                "threshold": default_threshold,
                            }
                            st.session_state["stable_configs"].append(new_cfg)
                            added_count += 1
                        
                        save_stable_configs(st.session_state["stable_configs"])
                        
                        # 更详细的成功提示
                        if added_count > 0:
                            st.success(f"✅ 成功添加 **{added_count}** 个交易对到监控配置！")
                            st.info("💡 提示：配置已保存，请查看主界面查看监控数据。页面将自动刷新...")
                            if skipped_count > 0:
                                st.info(f"ℹ️ 跳过 {skipped_count} 个已存在的配置：{', '.join(skipped_details[:5])}" + 
                                       (f" 等 {skipped_count} 个" if skipped_count > 5 else ""))
                            
                            # 重新加载配置，确保界面显示最新数据
                            st.session_state["stable_configs"] = load_stable_configs()
                            
                            # 更新采集结果缓存（移除已添加的项，保留未添加的）
                            remaining_pairs = []
                            for idx, p in enumerate(collected_pairs):
                                if idx not in selected_indices:
                                    # 未选中的保留
                                    remaining_pairs.append(p)
                                else:
                                    # 检查是否成功添加（可能因为已存在而跳过）
                                    exists = any(
                                        cfg.get("chain") == p["chain"] 
                                        and cfg.get("pair_address") == p["pair_address"]
                                        for cfg in st.session_state["stable_configs"]
                                    )
                                    if not exists:
                                        # 如果添加失败（可能因为已存在），也保留
                                        remaining_pairs.append(p)
                            
                            # 更新缓存
                            st.session_state["collected_pairs_cache"] = remaining_pairs
                            save_collected_pairs_cache(remaining_pairs)
                        else:
                            st.warning(f"⚠️ 没有添加任何交易对（所有 {skipped_count} 个都已存在）")
                        
                        # 清空选中状态（但保留采集结果，方便继续操作）
                        st.session_state["selected_pair_indices"] = []
                        st.rerun()
                
                with col_btn2:
                    if st.button("🗑️ 清空选择", use_container_width=True):
                        st.session_state["selected_pair_indices"] = []
                        st.rerun()
                
                with col_btn3:
                    if st.button("🔄 重新采集", use_container_width=True, help="清空当前结果，重新开始采集"):
                        st.session_state["collected_pairs_cache"] = []
                        st.session_state["selected_pair_indices"] = []
                        # 同时清空文件缓存
                        save_collected_pairs_cache([])
                        st.rerun()
            else:
                st.info("💡 提示：请从上方列表中选择要添加的交易对")
                
                # 如果采集结果不为空但没有选中项，显示清空按钮
                if collected_pairs:
                    if st.button("🗑️ 清空所有采集结果", use_container_width=True, help="清空采集结果缓存（包括文件）"):
                        st.session_state["collected_pairs_cache"] = []
                        st.session_state["selected_pair_indices"] = []
                        save_collected_pairs_cache([])
                        st.success("✅ 已清空所有采集结果")
                        st.rerun()
        
        st.markdown("---")

        existing_names = [c["name"] for c in st.session_state["stable_configs"]]
        selected_name = st.selectbox(
            "选择要编辑的稳定币（或输入新名称）",
            options=["<新建>"] + existing_names,
        )

        if selected_name != "<新建>":
            current_cfg = next(
                c for c in st.session_state["stable_configs"] if c["name"] == selected_name
            )
        else:
            current_cfg = {
                "name": "",
                "chain": "bsc",
                "pair_address": "",
                "anchor_price": default_anchor,
            }

        name_input = st.text_input(
            "交易对名称标识（如 USDT/USDC、USDT/USD0；同一交易对多链建议同名）",
            value=current_cfg["name"],
        )
        pair_input = st.text_input(
            "DexScreener 地址（可直接粘贴完整 URL，如 https://dexscreener.com/base/0x...）",
            value=current_cfg["pair_address"],
        )
        # 根据当前输入的 DexScreener 地址自动解析链标识，仅做回显，禁止手动修改，避免操作错误
        auto_chain, _ = parse_dexscreener_input(
            pair_input, current_cfg.get("chain", ""), current_cfg.get("pair_address", "")
        )
        chain_input = st.text_input(
            "链标识（自动从 URL 解析，仅供查看）",
            value=auto_chain,
            disabled=True,
        )
        anchor_input = st.number_input(
            "锚定价",
            min_value=0.1,
            max_value=10.0,
            value=float(current_cfg["anchor_price"]),
            step=0.01,
        )

        col_a, col_b = st.columns(2)
        if col_a.button("保存/更新配置"):
            if not name_input or not pair_input:
                st.warning("名称和 pair 地址不能为空。")
            else:
                # 支持直接粘贴完整 DexScreener URL / base/0x... / 纯 0x...
                parsed_chain, parsed_pair = parse_dexscreener_input(
                    pair_input, auto_chain, current_cfg.get("pair_address", "")
                )
                updated = {
                    "name": name_input,
                    "chain": parsed_chain,
                    "pair_address": parsed_pair,
                    "anchor_price": anchor_input,
                }
                # 如果 (name, chain) 已存在，更新；否则追加
                found = False
                for idx, cfg in enumerate(st.session_state["stable_configs"]):
                    if cfg["name"] == name_input and cfg["chain"] == chain_input:
                        st.session_state["stable_configs"][idx] = updated
                        found = True
                        break
                if not found:
                    st.session_state["stable_configs"].append(updated)
                # 保存到本地 JSON，供 CLI 共享使用
                save_stable_configs(st.session_state["stable_configs"])
                st.success(f"配置已保存到 {CONFIG_FILE}。如需 CLI 使用，请运行：python taoli.py")

        if col_b.button("删除当前配置") and selected_name != "<新建>":
            # 删除匹配 (name, chain) 的配置，而不是只删除同名的
            current_chain = current_cfg.get("chain")
            st.session_state["stable_configs"] = [
                c for c in st.session_state["stable_configs"] 
                if not (c["name"] == selected_name and c.get("chain") == current_chain)
            ]
            save_stable_configs(st.session_state["stable_configs"])
            st.success(f"已删除配置：{selected_name} ({current_chain})，并已更新 {CONFIG_FILE}")
            st.rerun()  # 刷新界面

    # ----- 主体：获取数据并展示 -----
    # 如果开启自动刷新，则通过 meta 标签让浏览器按间隔自动刷新页面
    if st.session_state.get("auto_refresh"):
        interval = max(5, int(st.session_state["check_interval"]))
        st.markdown(
            f"<meta http-equiv='refresh' content='{interval}'>",
            unsafe_allow_html=True,
        )
    stable_configs = st.session_state["stable_configs"]
    if not stable_configs:
        st.warning("当前没有任何监控配置，请在左侧面板添加至少一个稳定币。")
        return

    # 性能优化：使用进度条和缓存
    with st.spinner("正在获取稳定币数据..."):
        statuses = fetch_all_stable_status(
            stable_configs, global_threshold=st.session_state.get("global_threshold")
        )
    if not statuses:
        st.warning("当前未获取到任何稳定币数据，请检查配置是否正确。")
        return

    df = pd.DataFrame(statuses)
    df_display = df.copy()
    df_display["price"] = df_display["price"].map(lambda x: f"{x:.6f}")
    df_display["deviation_pct"] = df_display["deviation_pct"].map(lambda x: f"{x:+.3f}%")
    df_display["threshold"] = df_display["threshold"].map(lambda x: f"±{x:.3f}%")
    df_display["is_alert"] = df_display["is_alert"].map(lambda x: "是" if x else "否")

    alert_count = (df["is_alert"]).sum()
    
    # 缓存统计
    cache_stats = _global_cache.get_stats()
    
    # 漂亮的渐变卡片
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.markdown(f"""
        <div style='background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
                    padding: 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);'>
            <div style='color: white; font-size: 14px; opacity: 0.9;'>⚠️ 当前告警数量</div>
            <div style='color: white; font-size: 32px; font-weight: bold; margin-top: 5px;'>{int(alert_count)}</div>
        </div>
        """, unsafe_allow_html=True)
    
    with col2:
        st.markdown(f"""
        <div style='background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); 
                    padding: 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);'>
            <div style='color: white; font-size: 14px; opacity: 0.9;'>📊 监控总数</div>
            <div style='color: white; font-size: 32px; font-weight: bold; margin-top: 5px;'>{int(len(df))}</div>
        </div>
        """, unsafe_allow_html=True)
    
    with col3:
        st.markdown(f"""
        <div style='background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); 
                    padding: 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);'>
            <div style='color: white; font-size: 14px; opacity: 0.9;'>📈 最大偏离</div>
            <div style='color: white; font-size: 32px; font-weight: bold; margin-top: 5px;'>{df['deviation_pct'].abs().max():.3f}%</div>
        </div>
        """, unsafe_allow_html=True)
    
    with col4:
        st.markdown(f"""
        <div style='background: linear-gradient(135deg, #43e97b 0%, #38f9d7 100%); 
                    padding: 20px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);'>
            <div style='color: white; font-size: 14px; opacity: 0.9;'>⚡ 缓存命中率</div>
            <div style='color: white; font-size: 32px; font-weight: bold; margin-top: 5px;'>{cache_stats["hit_rate"]}</div>
            <div style='color: white; font-size: 11px; opacity: 0.8; margin-top: 5px;'>
                命中: {cache_stats['hits']} | 未命中: {cache_stats['misses']}
            </div>
        </div>
        """, unsafe_allow_html=True)

    # ----- 当前跨链套利机会（基于面板套利参数） -----
    st.markdown("---")
    st.subheader("💰 当前跨链套利机会")

    arb_opps = find_arbitrage_opportunities(
        statuses,
        trade_amount_usd=float(st.session_state["arb_trade_amount"]),
        src_gas_usd=float(st.session_state["arb_src_gas"]),
        dst_gas_usd=float(st.session_state["arb_dst_gas"]),
        bridge_fee_usd=float(st.session_state["arb_bridge_fee"]),
        slippage_pct=float(st.session_state["arb_slippage_pct"]),
        min_profit_usd=float(st.session_state["arb_min_profit_usd"]),
        min_profit_rate=float(st.session_state["arb_min_profit_rate"]),
        min_spread_pct=float(st.session_state["arb_min_spread_pct"]),
    )

    if arb_opps:
        # 统计不同状态的套利机会
        high_profit = [o for o in arb_opps if o["cost_detail"]["预估净利润"] > 100]
        medium_profit = [o for o in arb_opps if 10 <= o["cost_detail"]["预估净利润"] <= 100]
        low_profit = [o for o in arb_opps if o["cost_detail"]["预估净利润"] < 10]
        
        # 漂亮的状态指示卡片
        col_status1, col_status2, col_status3, col_status4 = st.columns(4)
        with col_status1:
            st.markdown(f"""
            <div style='text-align:center; padding:20px; 
                        background: linear-gradient(135deg, #56ab2f 0%, #a8e063 100%);
                        border-radius:12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);'>
                <span style='font-size:32px;'>🟢</span>
                <div style='color: white; font-size: 28px; font-weight: bold; margin-top: 10px;'>{len(high_profit)}</div>
                <div style='color: white; font-size: 14px; opacity: 0.9;'>高利润 (>$100)</div>
            </div>
            """, unsafe_allow_html=True)
        with col_status2:
            st.markdown(f"""
            <div style='text-align:center; padding:20px; 
                        background: linear-gradient(135deg, #f39c12 0%, #f1c40f 100%);
                        border-radius:12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);'>
                <span style='font-size:32px;'>🟡</span>
                <div style='color: white; font-size: 28px; font-weight: bold; margin-top: 10px;'>{len(medium_profit)}</div>
                <div style='color: white; font-size: 14px; opacity: 0.9;'>中利润 ($10-$100)</div>
            </div>
            """, unsafe_allow_html=True)
        with col_status3:
            st.markdown(f"""
            <div style='text-align:center; padding:20px; 
                        background: linear-gradient(135deg, #eb3349 0%, #f45c43 100%);
                        border-radius:12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);'>
                <span style='font-size:32px;'>🔴</span>
                <div style='color: white; font-size: 28px; font-weight: bold; margin-top: 10px;'>{len(low_profit)}</div>
                <div style='color: white; font-size: 14px; opacity: 0.9;'>低利润 (<$10)</div>
            </div>
            """, unsafe_allow_html=True)
        with col_status4:
            st.markdown(f"""
            <div style='text-align:center; padding:20px; 
                        background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
                        border-radius:12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);'>
                <span style='font-size:32px;'>📊</span>
                <div style='color: white; font-size: 28px; font-weight: bold; margin-top: 10px;'>{len(arb_opps)}</div>
                <div style='color: white; font-size: 14px; opacity: 0.9;'>总计</div>
            </div>
            """, unsafe_allow_html=True)
        
        st.markdown(
            f"<span style='color:green;font-weight:bold;'>当前有 {len(arb_opps)} 条跨链套利机会</span>",
            unsafe_allow_html=True,
        )
        
        # 初始化删除状态
        if "arb_to_delete" not in st.session_state:
            st.session_state["arb_to_delete"] = set()
        
        arb_rows = []
        for idx, opp in enumerate(arb_opps):
            cd = opp["cost_detail"]
            profit = cd["预估净利润"]
            
            # 根据利润确定状态颜色
            if profit > 100:
                status_icon = "🟢"
                status_text = "高利润"
            elif profit >= 10:
                status_icon = "🟡"
                status_text = "中利润"
            else:
                status_icon = "🔴"
                status_text = "低利润"
            
            arb_rows.append(
                {
                    "状态": f"{status_icon} {status_text}",
                    "稳定币": opp["name"],
                    "买入链": opp["cheap_chain"],
                    "卖出链": opp["rich_chain"],
                    "买入价(USD)": opp["cheap_price"],
                    "卖出价(USD)": opp["rich_price"],
                    "价差(%)": cd["价差百分比"],
                    "预估净利润(USD)": cd["预估净利润"],
                    "预估净利率(%)": cd["预估净利润率"],
                    "盈亏平衡资金规模(USD)": cd.get("盈亏平衡资金规模"),
                    "删除": False,  # 用于删除按钮
                    "_idx": idx,  # 内部索引
                }
            )
        
        df_arb = pd.DataFrame(arb_rows)
        df_arb_display = df_arb.copy()
        df_arb_display["买入价(USD)"] = df_arb_display["买入价(USD)"].map(
            lambda x: f"{x:.6f}"
        )
        df_arb_display["卖出价(USD)"] = df_arb_display["卖出价(USD)"].map(
            lambda x: f"{x:.6f}"
        )
        df_arb_display["价差(%)"] = df_arb_display["价差(%)"].map(
            lambda x: f"{x:+.3f}%"
        )
        df_arb_display["预估净利润(USD)"] = df_arb_display["预估净利润(USD)"].map(
            lambda x: f"{x:.2f}"
        )
        df_arb_display["预估净利率(%)"] = df_arb_display["预估净利率(%)"].map(
            lambda x: f"{x:+.3f}%"
        )
        if "盈亏平衡资金规模(USD)" in df_arb_display.columns:
            df_arb_display["盈亏平衡资金规模(USD)"] = df_arb_display[
                "盈亏平衡资金规模(USD)"
            ].map(lambda x: f"{x:.2f}" if isinstance(x, (int, float)) and x > 0 else "-")
        
        # 显示表格，每行添加删除按钮
        for idx, row in df_arb.iterrows():
            col_info, col_del = st.columns([10, 1])
            with col_info:
                # 显示该行的关键信息
                st.markdown(f"**{row['状态']}** | {row['稳定币']}: {row['买入链']} → {row['卖出链']} | 净利润: ${row['预估净利润(USD)']:.2f} ({row['预估净利率(%)']:+.3f}%)")
            with col_del:
                if st.button("🗑️", key=f"delete_arb_{idx}", help="删除此套利机会的监控配置"):
                    # 找到对应的监控配置并删除
                    opp = arb_opps[row["_idx"]]
                    name = opp["name"]
                    cheap_chain = opp["cheap_chain"]
                    rich_chain = opp["rich_chain"]
                    
                    # 删除相关的监控配置
                    removed = []
                    configs_to_keep = []
                    for cfg in st.session_state["stable_configs"]:
                        if cfg.get("name") == name and cfg.get("chain") in [cheap_chain, rich_chain]:
                            removed.append(f"{cfg.get('name')} ({cfg.get('chain')})")
                        else:
                            configs_to_keep.append(cfg)
                    
                    st.session_state["stable_configs"] = configs_to_keep
                    save_stable_configs(configs_to_keep)
                    if removed:
                        st.success(f"已删除 {len(removed)} 个相关监控配置: {', '.join(removed)}")
                    else:
                        st.info("未找到相关的监控配置")
                    st.rerun()
        
        # 也显示完整的数据表格（可选）
        with st.expander("📊 查看完整数据表格"):
            st.dataframe(df_arb_display.drop(columns=["删除", "_idx"]), width="stretch")
    else:
        st.markdown(
            "<span style='color:red;font-weight:bold;'>当前没有达到设定阈值的跨链套利机会</span>",
            unsafe_allow_html=True,
        )

    def highlight(row):
        # 兼容重命名前后的列名
        flag_col = "告警" if "告警" in row.index else "is_alert"
        return [
            "background-color: #ffcccc" if row[flag_col] == "是" else ""
            for _ in row
        ]

    st.subheader("📊 查看完整数据表格")
    
    # 高亮告警行的函数
    def highlight_alerts(row):
        if row["告警"] == "是":
            return ["background-color: #ffcccc"] * len(row)
        else:
            return [""] * len(row)
    
    # 准备显示数据
    df_display_table = df.copy()
    df_display_table["告警"] = df_display_table["is_alert"].map({True: "是", False: "否"})
    
    # 使用原生 dataframe（可排序、可筛选）
    st.dataframe(
        df_display_table[["name", "chain", "price", "deviation_pct", "threshold", "告警"]]
        .rename(columns={
            "name": "名称",
            "chain": "链",
            "price": "价格(USD)",
            "deviation_pct": "偏离(%)",
            "threshold": "阈值(%)",
        })
        .style.apply(highlight_alerts, axis=1),
        use_container_width=True,
        height=400,
    )
    
    # 删除功能区域（在表格下方）
    st.markdown("---")
    
    # 快速删除区域（可折叠）
    with st.expander("🗑️ 快速删除（点击展开）"):
        st.caption("直接点击删除，无需重新加载数据")
        
        # 使用多列布局显示删除按钮
        num_cols = 4
        num_rows = (len(df) + num_cols - 1) // num_cols
        
        for row_idx in range(num_rows):
            cols = st.columns(num_cols)
            for col_idx in range(num_cols):
                item_idx = row_idx * num_cols + col_idx
                if item_idx < len(df):
                    row = df.iloc[item_idx]
                    with cols[col_idx]:
                        # 根据价格判断是否可能是错误的
                        price = row['price']
                        is_suspicious = price > 2.0 or price < 0.5  # 稳定币应该接近 $1
                        
                        button_label = f"{'⚠️' if is_suspicious else '🗑️'} {row['name']}({row['chain']})"
                        button_help = f"价格: ${price:.4f}" + (" - 价格异常，可能不是稳定币" if is_suspicious else "")
                        
                        if st.button(button_label, key=f"quick_del_{item_idx}", 
                                   help=button_help, use_container_width=True):
                            # 快速删除
                            configs_to_keep = [
                                cfg for cfg in st.session_state["stable_configs"]
                                if not (cfg.get("name") == row["name"] and cfg.get("chain") == row["chain"])
                            ]
                            st.session_state["stable_configs"] = configs_to_keep
                            save_stable_configs(configs_to_keep)
                            st.success(f"✅ 已删除: {row['name']} ({row['chain']})")
                            st.rerun()
    
    # 原来的下拉删除方式（备用）
    with st.expander("🔽 下拉选择删除"):
        delete_options = [f"{row['name']} ({row['chain']}) - 价格: ${row['price']:.4f}" 
                          for _, row in df.iterrows()]
        
        if delete_options:
            col_select, col_btn = st.columns([3, 1])
            with col_select:
                selected_to_delete = st.selectbox(
                    "选择要删除的监控项",
                    options=delete_options,
                    key="delete_select"
                )
            with col_btn:
                st.write("")  # 占位，对齐按钮
                if st.button("🗑️ 删除", type="primary", use_container_width=True):
                    # 解析选中的项目
                    selected_idx = delete_options.index(selected_to_delete)
                    row_to_delete = df.iloc[selected_idx]
                    
                    name_to_delete = row_to_delete["name"]
                    chain_to_delete = row_to_delete["chain"]
                    
                    # 删除配置
                    configs_to_keep = [
                        cfg for cfg in st.session_state["stable_configs"]
                        if not (cfg.get("name") == name_to_delete and cfg.get("chain") == chain_to_delete)
                    ]
                    
                    st.session_state["stable_configs"] = configs_to_keep
                    save_stable_configs(configs_to_keep)
                    st.success(f"✅ 已删除: {name_to_delete} ({chain_to_delete})")
                    st.rerun()
        else:
            st.info("当前没有监控项")
    
    # 价格异常检测
    suspicious_items = df[((df['price'] > 2.0) | (df['price'] < 0.5))]
    if len(suspicious_items) > 0:
        st.error(f"⚠️ 检测到 {len(suspicious_items)} 个价格异常的项目（可能不是稳定币）！")
        
        # 一键清理所有异常
        col_warn, col_clean = st.columns([3, 1])
        with col_warn:
            st.write("**建议立即清理，这些可能是误添加的非稳定币（如ETH、BTC等）**")
        with col_clean:
            if st.button("🗑️ 一键清理所有异常", type="primary", use_container_width=True):
                # 收集所有异常项的 (name, chain)
                items_to_remove = set()
                for _, item in suspicious_items.iterrows():
                    items_to_remove.add((item['name'], item['chain']))
                
                # 从配置中删除
                configs_to_keep = [
                    cfg for cfg in st.session_state["stable_configs"]
                    if (cfg.get("name"), cfg.get("chain")) not in items_to_remove
                ]
                
                removed_count = len(st.session_state["stable_configs"]) - len(configs_to_keep)
                st.session_state["stable_configs"] = configs_to_keep
                save_stable_configs(configs_to_keep)
                
                st.success(f"✅ 已清理 {removed_count} 个异常配置！")
                st.rerun()
        
        st.markdown("---")
        
        # 显示异常项列表
        for idx, item in suspicious_items.iterrows():
            col1, col2 = st.columns([3, 1])
            with col1:
                st.error(f"**{item['name']} ({item['chain']})** - 价格: ${item['price']:.2f}")
            with col2:
                if st.button(f"删除", key=f"del_suspicious_{idx}", use_container_width=True):
                    configs_to_keep = [
                        cfg for cfg in st.session_state["stable_configs"]
                        if not (cfg.get("name") == item["name"] and cfg.get("chain") == item["chain"])
                    ]
                    st.session_state["stable_configs"] = configs_to_keep
                    save_stable_configs(configs_to_keep)
                    st.success(f"已删除: {item['name']}")
                    st.rerun()
    
    # 调试：显示当前配置
    with st.expander("🔍 调试信息 - 查看当前配置"):
        st.write(f"**配置文件路径:** `{CONFIG_FILE}`")
        st.write(f"**Session 中配置数量:** {len(st.session_state['stable_configs'])}")
        
        # 显示所有配置
        if st.session_state['stable_configs']:
            config_display = []
            for idx, cfg in enumerate(st.session_state['stable_configs']):
                config_display.append({
                    "序号": idx,
                    "名称": cfg.get("name"),
                    "链": cfg.get("chain"),
                    "Pair地址": cfg.get("pair_address", "")[:20] + "...",
                })
            st.dataframe(pd.DataFrame(config_display), use_container_width=True)
            
            # 查看原始 JSON
            if st.checkbox("查看原始 JSON 配置"):
                st.json(st.session_state['stable_configs'])
        else:
            st.write("配置为空")
        
        # 操作按钮
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🔄 从文件重新加载配置"):
                reloaded = load_stable_configs()
                st.session_state["stable_configs"] = reloaded
                st.success(f"已从文件重新加载 {len(reloaded)} 个配置")
                st.rerun()
        
        with col2:
            if st.button("🗑️ 清空所有配置"):
                st.session_state["stable_configs"] = []
                save_stable_configs([])
                st.success("已清空所有配置")
                st.rerun()

    # ----- 仪表 & 曲线 -----
    # 更新历史数据
    now_ts = pd.Timestamp(now_beijing())
    history_df = st.session_state["history"]
    new_rows = pd.DataFrame(
        [
            {
                "timestamp": now_ts,
                "name": s["name"],
                "symbol": (s.get("symbol") or "").upper(),
                "chain": s["chain"],
                "price": s["price"],
                "deviation_pct": s["deviation_pct"],
            }
            for s in statuses
        ]
    )
    history_df = pd.concat([history_df, new_rows], ignore_index=True)
    
    # 数据清理策略：保留最近 HISTORY_MAX_RECORDS 条或最近 24 小时的数据
    if len(history_df) > HISTORY_MAX_RECORDS:
        # 方法1：按数量限制
        history_df = history_df.iloc[-HISTORY_MAX_RECORDS:]
        logger.debug(f"历史数据已清理，保留最近 {HISTORY_MAX_RECORDS} 条")
    
    # 方法2：按时间窗口清理（可选，取消注释启用）
    # cutoff_time = now_ts - pd.Timedelta(hours=24)
    # history_df = history_df[history_df['timestamp'] >= cutoff_time]
    
    st.session_state["history"] = history_df
    logger.debug(f"历史数据已更新，当前 {len(history_df)} 条记录")

    st.markdown("---")
    st.subheader("🎛️ 关键稳定币仪表")
    
    # 按偏离度排序，显示所有稳定币（优化：限制显示数量，避免卡顿）
    max_display = min(20, len(df))  # 最多显示20个，避免页面卡顿
    sorted_df = df.sort_values("deviation_pct", key=lambda s: s.abs(), ascending=False).head(max_display)
    
    # 使用多列布局，每行显示4个
    num_cols = 4
    num_rows = (max_display + num_cols - 1) // num_cols
    
    for row_idx in range(num_rows):
        cols = st.columns(num_cols)
        for col_idx in range(num_cols):
            item_idx = row_idx * num_cols + col_idx
            if item_idx < len(sorted_df):
                row = sorted_df.iloc[item_idx]
                with cols[col_idx]:
                    # 根据偏离度设置颜色
                    dev_abs = abs(row['deviation_pct'])
                    if dev_abs >= row['threshold']:
                        bg_color = "#ffe6e6"
                        border_color = "#e74c3c"
                        text_color = "#e74c3c"
                    elif dev_abs >= row['threshold'] * 0.5:
                        bg_color = "#fff9e6"
                        border_color = "#f39c12"
                        text_color = "#f39c12"
                    else:
                        bg_color = "#e8f8f5"
                        border_color = "#2ecc71"
                        text_color = "#2ecc71"
                    
                    # 自定义卡片，数字更小
                    st.markdown(f"""
                    <div style='background: {bg_color}; 
                                border-left: 4px solid {border_color};
                                padding: 10px;
                                border-radius: 5px;
                                margin-bottom: 10px;'>
                        <div style='font-size: 12px; color: #666; margin-bottom: 5px;'>
                            {row['name']} ({row['chain']})
                        </div>
                        <div style='font-size: 20px; font-weight: bold; color: {text_color};'>
                            {row['deviation_pct']:+.3f}%
                        </div>
                        <div style='font-size: 11px; color: #999; margin-top: 3px;'>
                            ${row['price']:.4f} USD
                        </div>
                    </div>
                    """, unsafe_allow_html=True)
    
    if len(df) > max_display:
        st.caption(f"显示前 {max_display} 个偏离度最大的稳定币（共 {len(df)} 个）")

    st.subheader("📈 价格 vs 1 美金 对比曲线")
    symbols_available = sorted(
        { (s.get("symbol") or "").upper() for s in statuses if s.get("symbol") }
    )
    
    # 从持久化配置中加载已选择的符号
    saved_selected = st.session_state.get("selected_symbols", [])
    # 过滤掉不存在的符号（可能已删除）
    valid_saved = [s for s in saved_selected if s in symbols_available]
    # 如果没有保存的选择，使用默认值
    default_selected = valid_saved if valid_saved else (symbols_available[:2] if symbols_available else [])
    
    selected_symbols = st.multiselect(
        "选择要查看曲线的稳定币（按币种，多链聚合，可多选）",
        options=symbols_available,
        default=default_selected,
        key="symbols_multiselect",
    )
    
    # 当选择改变时自动保存
    if selected_symbols != st.session_state.get("selected_symbols", []):
        st.session_state["selected_symbols"] = selected_symbols
        # 自动保存到配置文件
        gcfg = load_global_config()
        if "ui_config" not in gcfg:
            gcfg["ui_config"] = {}
        gcfg["ui_config"]["selected_symbols"] = selected_symbols
        save_global_config(gcfg)

    if not history_df.empty and selected_symbols:
        for sym in selected_symbols:
            sub = history_df[history_df["symbol"] == sym].sort_values("timestamp")
            if sub.empty:
                continue

            # 使用 Plotly 画多链价格对比，可交互缩放时间轴
            fig_df = sub.copy()
            fig_df["timestamp"] = pd.to_datetime(fig_df["timestamp"])

            fig = px.line(
                fig_df,
                x="timestamp",
                y="price",
                color="chain",
                labels={
                    "timestamp": "时间（北京时间）",
                    "price": "价格(USD)",
                    "chain": "链",
                },
                title=f"{sym} 多链价格对比（含 1 USD 锚定线）",
            )
            # 稳定币价格一般在 0~几美金之间，将 Y 轴固定在 0~2 区间，避免出现看起来很吓人的大数刻度
            fig.update_yaxes(range=[0, 2])
            # 添加 1 USD 锚定线
            if not fig_df.empty:
                fig.add_hline(
                    y=1.0,
                    line_dash="dash",
                    line_color="gray",
                    annotation_text="1 USD",
                    annotation_position="top left",
                )

            st.plotly_chart(fig, width="stretch")

    st.markdown("---")
    st.subheader("🧮 跨链套利成本计算器")

    # 选择源链和目标链（基于当前监控项）
    names_for_calc = [f"{s['name']} ({s['chain']})" for s in statuses]
    src_sel = st.selectbox("源链稳定币（买入所在链）", options=names_for_calc, key="src_sel")
    dst_sel = st.selectbox("目标链稳定币（卖出所在链）", options=names_for_calc, key="dst_sel")

    src_idx = names_for_calc.index(src_sel)
    dst_idx = names_for_calc.index(dst_sel)
    src_status = statuses[src_idx]
    dst_status = statuses[dst_idx]

    col_amt, col_sgas, col_dgas = st.columns(3)
    trade_amount = col_amt.number_input(
        "计划套利资金规模（USD）",
        min_value=10.0,
        max_value=1_000_000.0,
        value=1000.0,
        step=10.0,
    )
    
    # 源链 gas 费用输入，带自动获取功能
    src_chain_id = CHAIN_NAME_TO_ID.get(src_status['chain'])
    src_gas_col1, src_gas_col2 = st.columns([3, 1])
    with src_gas_col1:
        src_gas = st.number_input(
            f"源链 {src_status['chain']} 预估总 Gas（USD）",
            min_value=0.0,
            max_value=100.0,
            value=1.0,
            step=0.1,
            key="src_gas_input",
        )
    with src_gas_col2:
        if st.button("获取 Gas", key="get_src_gas", help="从 LI.FI API 获取当前链的 gas 价格"):
            if src_chain_id:
                gas_prices = get_lifi_gas_prices(src_chain_id)
                if gas_prices:
                    # 使用 fast 价格，假设 gas limit 为 100000
                    estimated_gas = estimate_gas_cost_usd(
                        src_chain_id,
                        gas_price_gwei=gas_prices.get("fast"),
                        gas_limit=100000
                    )
                    if estimated_gas:
                        st.session_state["src_gas_input"] = estimated_gas
                        st.success(f"已获取: ${estimated_gas:.2f}")
                    else:
                        st.warning("无法估算 gas 费用")
                else:
                    st.warning(f"无法获取链 {src_status['chain']} 的 gas 价格")
            else:
                st.warning(f"链 {src_status['chain']} 不在 chainId 映射表中")
        src_gas = st.session_state.get("src_gas_input", src_gas)
    
    # 目标链 gas 费用输入，带自动获取功能
    dst_chain_id = CHAIN_NAME_TO_ID.get(dst_status['chain'])
    dst_gas_col1, dst_gas_col2 = st.columns([3, 1])
    with dst_gas_col1:
        dst_gas = st.number_input(
            f"目标链 {dst_status['chain']} 预估总 Gas（USD）",
            min_value=0.0,
            max_value=100.0,
            value=1.0,
            step=0.1,
            key="dst_gas_input",
        )
    with dst_gas_col2:
        if st.button("获取 Gas", key="get_dst_gas", help="从 LI.FI API 获取当前链的 gas 价格"):
            if dst_chain_id:
                gas_prices = get_lifi_gas_prices(dst_chain_id)
                if gas_prices:
                    estimated_gas = estimate_gas_cost_usd(
                        dst_chain_id,
                        gas_price_gwei=gas_prices.get("fast"),
                        gas_limit=100000
                    )
                    if estimated_gas:
                        st.session_state["dst_gas_input"] = estimated_gas
                        st.success(f"已获取: ${estimated_gas:.2f}")
                    else:
                        st.warning("无法估算 gas 费用")
                else:
                    st.warning(f"无法获取链 {dst_status['chain']} 的 gas 价格")
            else:
                st.warning(f"链 {dst_status['chain']} 不在 chainId 映射表中")
        dst_gas = st.session_state.get("dst_gas_input", dst_gas)

    col_bridge, col_slip, _ = st.columns(3)
    bridge_fee = col_bridge.number_input(
        "跨链桥费用（USD）",
        min_value=0.0,
        max_value=100.0,
        value=DEFAULT_BRIDGE_FEE_USD,
        step=0.5,
    )
    slippage_pct = col_slip.number_input(
        "往返滑点总和（%）",
        min_value=0.0,
        max_value=20.0,
        value=DEFAULT_SLIPPAGE_PCT,
        step=0.1,
    )

    if st.button("计算套利净利润"):
        # 先用面板参数做一遍基础成本估算
        cost_detail = calculate_arbitrage_cost(
            trade_amount_usd=trade_amount,
            src_price=src_status["price"],
            dst_price=dst_status["price"],
            src_chain=src_status["chain"],
            dst_chain=dst_status["chain"],
            src_gas_usd=src_gas,
            dst_gas_usd=dst_gas,
            bridge_fee_usd=bridge_fee,
            slippage_pct=slippage_pct,
        )

        # 再尝试用 LI.FI 实时报价对结果做二次精算
        # - 代码会尝试调用 LI.FI API，根据实际响应判断是否支持该链对
        # - 如果该链对被 LI.FI 支持，净利润/净利率/总成本会更贴近真实
        # - 如果不支持或请求失败，则保留基础估算结果，并显示具体原因
        cost_detail = refine_cost_with_lifi(
            src_status=src_status,
            dst_status=dst_status,
            trade_amount_usd=trade_amount,
            base_cost_detail=cost_detail,
        )

        st.write("**套利成本与利润估算（USD）**")
        # 标记成本来源：是完全基于面板参数的估算，还是已被 LI.FI 实时报价二次精算
        if cost_detail.get("LI.FI_数据来源"):
            st.success(f"✅ 成本来源: 基于面板参数 + {cost_detail['LI.FI_数据来源']} 实时报价精算")
            if "LI.FI_到手数量" in cost_detail:
                st.write(f"- LI.FI 预估到手稳定币数量: {cost_detail['LI.FI_到手数量']}")
            if cost_detail.get("LI.FI_费用数据完整"):
                st.info("💡 费用明细已从 LI.FI 路由中自动提取（Gas、跨链桥费、手续费、滑点损失等）")
        else:
            skip_reason = cost_detail.get("LI.FI_跳过原因")
            if skip_reason:
                st.warning(f"⚠️ 成本来源: 完全基于当前面板参数的估算（LI.FI 精算跳过原因：{skip_reason}）")
            else:
                st.info("ℹ️ 成本来源: 完全基于当前面板参数的估算（未获取到聚合器实时报价）")
        
        # 显示成本明细，标记哪些来自 LI.FI API
        cost_items = [
            "价差百分比",
            "理论价差利润",
            "Gas费（源链）",
            "Gas费（目标链）",
            "跨链桥费",
            "其他手续费",
            "滑点损失",
            "总成本",
            "预估净利润",
            "预估净利润率",
        ]
        
        for k in cost_items:
            v = cost_detail.get(k)
            if v is None:
                continue
            
            # 检查是否来自 LI.FI API
            lifi_marker = ""
            if k == "Gas费（源链）" and cost_detail.get("LI.FI_源链Gas来源"):
                lifi_marker = " 🔵"
            elif k == "Gas费（目标链）" and cost_detail.get("LI.FI_目标链Gas来源"):
                lifi_marker = " 🔵"
            elif k == "跨链桥费" and cost_detail.get("LI.FI_跨链桥费来源"):
                lifi_marker = " 🔵"
            elif k == "其他手续费" and cost_detail.get("LI.FI_其他手续费来源"):
                lifi_marker = " 🔵"
            elif k == "滑点损失" and cost_detail.get("LI.FI_滑点损失来源"):
                lifi_marker = " 🔵"
            
            if "百分比" in k or "率" in k:
                st.write(f"- {k}: {v}%{lifi_marker}")
            else:
                st.write(f"- {k}: ${v}{lifi_marker}")
        
        # 显示滑点百分比（如果从 LI.FI 获取到了）
        if cost_detail.get("滑点百分比") is not None:
            slippage_pct = cost_detail.get("滑点百分比")
            slippage_source = cost_detail.get("LI.FI_滑点百分比来源", "")
            marker = " 🔵" if slippage_source else ""
            st.write(f"- 滑点百分比: {slippage_pct}%{marker}")
        
        # 显示费用来源说明
        lifi_sources = []
        if cost_detail.get("LI.FI_源链Gas来源"):
            lifi_sources.append("源链 Gas")
        if cost_detail.get("LI.FI_目标链Gas来源"):
            lifi_sources.append("目标链 Gas")
        if cost_detail.get("LI.FI_跨链桥费来源"):
            lifi_sources.append("跨链桥费")
        if cost_detail.get("LI.FI_其他手续费来源"):
            lifi_sources.append("其他手续费")
        if cost_detail.get("LI.FI_滑点损失来源"):
            lifi_sources.append("滑点损失")
        
        if lifi_sources:
            st.caption(f"🔵 标记的费用项来自 LI.FI API: {', '.join(lifi_sources)}")

        if cost_detail["预估净利润"] > 0:
            st.success("在当前参数下，该跨链套利机会**理论上可行**（净利润为正）。")
        else:
            st.warning("在当前参数下，该跨链套利机会**不划算**（成本吃掉了价差）。")

    # 面板内不再自动直接发脱锚告警；所有告警/套利/心跳统一由 CLI + 用户管理负责分发。
    
    st.markdown("---")
    st.subheader("📤 发送日志")
    
    # 显示今日发送统计（只统计 Server酱，因为只有 Server酱 有限制）
    serverchan_count = get_today_send_count("Server酱")
    serverchan_remaining = MAX_DAILY_SENDS - serverchan_count
    
    col_stat1, col_stat2, col_stat3 = st.columns(3)
    col_stat1.metric("Server酱已发送", f"{serverchan_count} 条")
    col_stat2.metric("Server酱剩余", f"{serverchan_remaining} 条")
    col_stat3.metric("Server酱限额", f"{MAX_DAILY_SENDS} 条/天")
    
    st.caption(f"💡 心跳: 每天{HEARTBEAT_PER_DAY}次（{HEARTBEAT_INTERVAL/3600:.1f}小时间隔）")
    st.caption(f"⚡ 套利专用额度: {ARBITRAGE_QUOTA}条/天（仅 Server酱）")
    st.caption("📌 策略: 套利优先，心跳避让，确保不错过赚钱机会")
    st.info("ℹ️ **重要提示**: Server酱每天限制 5 条；Telegram 和钉钉无限制，可随时发送")
    
    # 显示发送日志列表
    logs = load_send_log()
    if logs:
        st.markdown("**最近发送记录：**")
        
        # 倒序显示（最新的在前）
        logs_reversed = list(reversed(logs[-20:]))  # 只显示最近20条
        
        for log in logs_reversed:
            msg_type = log.get("type", "未知")
            content = log.get("content", "")
            channels = log.get("channels", [])
            success = log.get("success", True)
            time_str = log.get("time", "")
            
            # 根据类型设置图标
            type_icon = {
                "心跳": "💓",
                "脱锚告警": "⚠️",
                "套利机会": "💰",
                "测试": "🧪"
            }.get(msg_type, "📨")
            
            # 根据成功状态设置颜色
            status_icon = "✅" if success else "❌"
            
            with st.expander(f"{type_icon} {msg_type} - {time_str} {status_icon}"):
                st.text(content)
                st.caption(f"发送渠道: {', '.join(channels) if channels else '无'}")
    else:
        st.info("暂无发送记录")
    
    # 清空日志按钮
    if st.button("🗑️ 清空发送日志"):
        save_send_log([])
        st.success("已清空发送日志")
        st.rerun()


# ========== 入口选择 ==========
#
# 说明：
# - 使用 `python taoli.py cli` 启动 CLI 监控（后台长期运行）
# - 使用 `streamlit run taoli.py` 启动可视化面板（不自动跑 CLI）

if __name__ == "__main__":
    # 显式带参数 "cli" 时，运行命令行监控；否则默认认为是面板模式
    if len(sys.argv) > 1 and sys.argv[1].lower() == "cli":
        run_cli_monitor_with_alerts()
    else:
        run_streamlit_panel()
