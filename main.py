#!/usr/bin/env python3
"""
京东商品价格/库存监控程序
支持定时检查商品价格和库存变化，并通过飞书发送通知
"""

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import yaml
from apscheduler.schedulers.blocking import BlockingScheduler

# 配置日志
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "jd_monitor.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# 状态存储文件
STATE_FILE = Path(__file__).parent / "state.json"


class Config:
    """配置管理"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self._config = self._load_config()

    def _load_config(self) -> dict:
        """加载配置文件"""
        config_file = Path(__file__).parent / self.config_path
        if not config_file.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_file}")

        with open(config_file, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        if not config:
            raise ValueError("配置文件为空")

        if "products" not in config:
            raise ValueError("配置缺少 products 字段")

        products = config.get("products", [])
        if len(products) > 5:
            raise ValueError("最多支持5个商品")

        if len(products) < 1:
            raise ValueError("至少需要配置1个商品")

        return config

    @property
    def interval(self) -> int:
        return self._config.get("monitor", {}).get("interval", 300)

    @property
    def products(self) -> list:
        return self._config.get("products", [])

    @property
    def feishu_webhook(self) -> Optional[str]:
        return self._config.get("feishu", {}).get("webhook_url")

    @property
    def jd_cookie(self) -> Optional[str]:
        jd_config = self._config.get("jd") or {}
        return jd_config.get("cookie")


class JDMoitor:
    """京东商品监控"""

    def __init__(self, config: Config):
        self.config = config
        self.state = self._load_state()
        self._playwright = None
        self._browser = None
        self._cookie = config.jd_cookie

    def _load_state(self) -> dict:
        """加载上次状态"""
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"加载状态文件失败: {e}")
        return {}

    def _save_state(self) -> None:
        """保存当前状态"""
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存状态文件失败: {e}")

    def _init_playwright(self):
        """初始化Playwright"""
        if self._playwright is None:
            try:
                from playwright.sync_api import sync_playwright
                self._playwright = sync_playwright().start()
                self._browser = self._playwright.chromium.launch(
                    headless=True,
                    args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
                )
                logger.info("Playwright 初始化成功")
            except Exception as e:
                logger.error(f"Playwright 初始化失败: {e}")
                raise

    def _close_playwright(self):
        """关闭Playwright"""
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()
        self._playwright = None
        self._browser = None

    def _get_price_and_stock(self, sku: str) -> tuple:
        """使用Playwright获取商品价格和库存"""
        try:
            self._init_playwright()

            page = self._browser.new_page()

            # 设置Cookie
            if self._cookie:
                # 解析cookie字符串
                cookies = []
                for item in self._cookie.split(";"):
                    item = item.strip()
                    if "=" in item:
                        key, value = item.split("=", 1)
                        cookies.append({
                            "name": key,
                            "value": value,
                            "domain": ".jd.com",
                            "path": "/"
                        })
                page.context.add_cookies(cookies)
                logger.info("已设置京东Cookie")

            page.set_extra_http_headers(HEADERS)

            # 访问商品页面
            url = f"https://item.jd.com/{sku}.html"
            page.goto(url, wait_until="domcontentloaded", timeout=20000)

            # 等待页面加载
            page.wait_for_timeout(2000)

            # 检查是否跳转到登录页
            if "login" in page.url.lower() or "登录" in page.content()[:500]:
                logger.error("需要登录，请配置京东Cookie")
                page.close()
                return None, None

            # 获取页面内容
            content = page.content()

            # 提取价格
            price = None
            price_patterns = [
                r'"price":{"m":"\d+","p":"(\d+\.?\d*)"',
                r'"lowestPrice":(\d+\.?\d*)',
                r'"price":"(\d+\.?\d*)"',
                r'¥(\d+\.?\d*)',
                r'class="price"[^>]*>(\d+\.?\d*)',
            ]
            for pattern in price_patterns:
                match = re.search(pattern, content)
                if match:
                    price = float(match.group(1))
                    break

            # 提取库存状态
            in_stock = None

            # 检查是否有货
            if "无货" in content or "已售罄" in content or "缺货" in content:
                in_stock = False
            elif "立即购买" in content or "加入购物车" in content:
                in_stock = True

            # 尝试从JavaScript变量中提取
            if price is None or in_stock is None:
                # 尝试提取JSON数据
                json_pattern = r'window\.__INITIAL_STATE__\s*=\s*({.+?});'
                match = re.search(json_pattern, content)
                if match:
                    try:
                        data = json.loads(match.group(1))
                        if price is None:
                            price_info = data.get("product", {}).get("priceInfo", {})
                            price = price_info.get("lowestPrice") or price_info.get("price")
                        if in_stock is None:
                            stock_info = data.get("product", {}).get("stockInfo", {})
                            in_stock = stock_info.get("canBuy", stock_info.get("isInStock"))
                    except:
                        pass

            page.close()

            return price, in_stock

        except Exception as e:
            logger.error(f"Playwright获取数据失败: {sku}, {e}")
            return None, None

    def _get_product_name(self, sku: str) -> str:
        """获取商品名称"""
        try:
            # 尝试从缓存获取
            if self._browser:
                page = self._browser.new_page()
                page.set_extra_http_headers(HEADERS)
                page.goto(f"https://item.jd.com/{sku}.html", wait_until="domcontentloaded", timeout=15000)
                content = page.content()

                patterns = [
                    r'<title>([^<]+)</title>',
                    r'"name":"([^"]+)"',
                    r'"productName":"([^"]+)"',
                ]
                for pattern in patterns:
                    match = re.search(pattern, content)
                    if match:
                        name = match.group(1).replace("【行情_报价_价格】", "").strip()
                        page.close()
                        return name

                page.close()

            return f"商品{sku}"

        except Exception as e:
            logger.warning(f"获取商品名称失败: {sku}, {e}")
            return f"商品{sku}"

    def check_product(self, product: dict) -> dict:
        """检查单个商品"""
        sku = product.get("sku")
        name = product.get("name")

        if not sku:
            logger.error(f"商品缺少sku: {product}")
            return {}

        logger.info(f"检查商品: {sku}")

        # 使用Playwright获取价格和库存
        price, stock = self._get_price_and_stock(sku)

        # 如果没有名称，尝试获取
        if not name:
            name = self._get_product_name(sku)

        result = {
            "name": name,
            "sku": sku,
            "price": price,
            "in_stock": stock,
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        logger.info(f"检查结果: {result}")

        return result

    def check_all_products(self) -> list:
        """检查所有商品"""
        results = []
        try:
            for product in self.config.products:
                result = self.check_product(product)
                if result:
                    results.append(result)
                # 避免请求太频繁
                time.sleep(2)
        finally:
            self._close_playwright()

        return results

    def check_changes(self) -> list:
        """检查是否有变化并返回变化列表"""
        changes = []
        results = self.check_all_products()

        for result in results:
            sku = result["sku"]
            prev = self.state.get(sku, {})

            # 检查价格变化
            if prev.get("price") != result["price"]:
                changes.append({
                    "type": "price",
                    "product": result["name"],
                    "sku": sku,
                    "old_price": prev.get("price"),
                    "new_price": result["price"],
                })

            # 检查库存变化
            if prev.get("in_stock") != result["in_stock"]:
                stock_status = "有货" if result["in_stock"] else "无货"
                old_stock = "有货" if prev.get("in_stock") else "无货"
                changes.append({
                    "type": "stock",
                    "product": result["name"],
                    "sku": sku,
                    "old_status": old_stock,
                    "new_status": stock_status,
                })

            # 更新状态
            self.state[sku] = result

        # 保存状态
        self._save_state()

        return changes


class FeishuNotifier:
    """飞书通知"""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, message: str) -> bool:
        """发送消息"""
        if not self.webhook_url:
            logger.warning("未配置飞书webhook，跳过通知")
            return False

        try:
            payload = {
                "msg_type": "text",
                "content": {
                    "text": message
                }
            }

            response = requests.post(
                self.webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )

            if response.status_code == 200:
                result = response.json()
                if result.get("code") == 0:
                    logger.info("飞书通知发送成功")
                    return True
                else:
                    logger.error(f"飞书通知失败: {result}")
                    return False
            else:
                logger.error(f"飞书通知HTTP错误: {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"飞书通知发送异常: {e}")
            return False

    def send_price_change(self, product: str, old_price: float, new_price: float) -> bool:
        """发送价格变动通知"""
        change = new_price - old_price
        direction = "上涨" if change > 0 else "下降"

        message = f"""【价格变动】
商品: {product}
价格: {old_price:.2f} → {new_price:.2f} ({direction}{abs(change):.2f}元)
时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}"""

        return self.send(message)

    def send_stock_change(self, product: str, old_status: str, new_status: str) -> bool:
        """发送库存变动通知"""
        message = f"""【库存变动】
商品: {product}
状态: {old_status} → {new_status}
时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}"""

        return self.send(message)


def main():
    """主函数"""
    logger.info("=" * 50)
    logger.info("京东商品监控程序启动")
    logger.info("=" * 50)

    # 加载配置
    config = Config()
    logger.info(f"商品数量: {len(config.products)}")
    logger.info(f"监控时间: 每天 8:00-22:00，每小时检查一次")

    # 初始化
    monitor = JDMoitor(config)
    notifier = FeishuNotifier(config.feishu_webhook) if config.feishu_webhook else None

    def job():
        """定时任务"""
        now = datetime.now()
        hour = now.hour

        # 检查是否在监控时间范围内 (8:00-22:00)
        if hour < 8 or hour >= 22:
            logger.info(f"当前时间 {hour}:00，不在监控时间范围内 (8:00-22:00)，跳过")
            return

        logger.info("-" * 30)
        logger.info(f"开始检查: {now.strftime('%Y-%m-%d %H:%M:%S')}")

        try:
            changes = monitor.check_changes()

            if not notifier:
                logger.info("未配置通知，跳过")
                return

            if not changes:
                logger.info("没有变化")
                return

            # 发送通知
            for change in changes:
                if change["type"] == "price":
                    notifier.send_price_change(
                        change["product"],
                        change["old_price"],
                        change["new_price"]
                    )
                elif change["type"] == "stock":
                    notifier.send_stock_change(
                        change["product"],
                        change["old_status"],
                        change["new_status"]
                    )

            logger.info(f"已发送{len(changes)}条通知")

        except Exception as e:
            logger.error(f"检查异常: {e}")

    # 立即执行一次
    job()

    # 启动定时任务 - 每小时执行一次
    scheduler = BlockingScheduler()
    scheduler.add_job(job, "cron", hour="8-21", minute=0, id="jd_monitor")

    logger.info("定时任务已启动，每天 8:00-21:00 每小时检查一次")
    logger.info("按 Ctrl+C 停止")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("程序已停止")


if __name__ == "__main__":
    main()
