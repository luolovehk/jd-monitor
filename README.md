# 京东商品监控程序

定时监控京东商品的价格和库存，通过飞书发送通知。

## 功能特性

- 监控 1-5 个京东商品
- 定时检查价格变化
- 监控库存状态（有货/无货）
- 飞书机器人通知
- Docker 容器化部署

## 文件结构

```
jd-monitor/
├── Dockerfile          # Docker镜像
├── requirements.txt    # Python依赖
├── config.yaml         # 配置文件
├── main.py            # 主程序
├── README.md          # 说明文档
└── logs/              # 日志目录
```

## 配置说明

编辑 `config.yaml`:

```yaml
monitor:
  interval: 300  # 检查间隔（秒），默认5分钟

products:
  # 商品配置（支持1-5个商品）
  - name: "iPhone 16 Pro"
    sku: "100123456789"  # 商品SKU ID

  - name: "AirPods Pro"
    sku: "100008348543"

feishu:
  # 飞书机器人webhook地址
  webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
```

### 获取商品SKU

1. 打开京东商品页面
2. 查看URL中的数字ID，例如：
   - `https://item.jd.com/100123456789.html` → SKU: `100123456789`
   - 移动端：`https://item.m.jd.com/product/100123456789.html`

### 获取飞书Webhook

1. 打开飞书群设置 → 群机器人 → 添加机器人 → 自定义机器人
2. 设置机器人名称
3. 复制 webhook 地址

## 使用方式

### 方式一：Docker 部署

```bash
# 构建镜像
docker build -t jd-monitor .

# 运行容器（挂载配置文件）
docker run -d \
  --name jd-monitor \
  -v $(pwd)/config.yaml:/app/config.yaml \
  jd-monitor

# 查看日志
docker logs -f jd-monitor

# 停止
docker stop jd-monitor
```

### 方式二：本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 运行
python main.py
```

## 查看日志

```bash
# 实时查看日志
docker logs -f jd-monitor

# 或查看日志文件
cat logs/jd_monitor.log
```

## 注意事项

1. 首次运行会创建 `state.json` 文件，用于保存商品状态
2. 价格和库存数据来源于京东移动端API，可能有延迟
3. 建议检查间隔不要太短（至少60秒），避免被京东限制
4. 程序会记录每次检查结果到日志文件
