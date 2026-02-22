#!/bin/bash
# 全球新闻定时推送封装脚本
# 用于cron定时执行和邮件推送

set -eo pipefail

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 配置
LOG_DIR="$SCRIPT_DIR/logs"
CONFIG_FILE="news-sources-config.json"
PYTHON_SCRIPT="unified-global-news-sender.py"

# 创建日志目录
mkdir -p "$LOG_DIR"

# 日志文件
LOG_FILE="$LOG_DIR/news-sender-$(date +%Y%m%d).log"

# 读取配置（从.stock-monitor.env或使用默认值）
if [ -f ~/.stock-monitor.env ]; then
    source ~/.stock-monitor.env
else
    MAIL_TO="${MAIL_TO:-}"
    SMTP_USER="${SMTP_USER:-}"
    SMTP_PASS="${SMTP_PASS:-}"
fi

# 日志函数
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# 错误处理
error_exit() {
    log "❌ 错误: $1"
    exit 1
}

# 检查配置
check_config() {
    log "🔍 检查配置..."
    
    if [ ! -f "$CONFIG_FILE" ]; then
        error_exit "配置文件不存在: $CONFIG_FILE"
    fi
    
    if [ ! -f "$PYTHON_SCRIPT" ]; then
        error_exit "Python脚本不存在: $PYTHON_SCRIPT"
    fi
    
    log "✅ 配置检查完毕"
}

# 检查新闻源可访问性
check_sources() {
    log "🔍 检查新闻源可访问性..."
    python3 "$PYTHON_SCRIPT" check 2>&1 | tee -a "$LOG_FILE" | grep -E "✅|❌" || true
    log "✅ 新闻源检查完毕"
}

# 执行推送（控制台模式）
run_console_mode() {
    log "📻 运行控制台模式..."
    python3 "$PYTHON_SCRIPT" console 2>&1 | tee -a "$LOG_FILE" || {
        log "❌ 控制台模式执行失败 (exit=${PIPESTATUS[0]})"
        return 1
    }
    log "✅ 控制台模式完毕"
}

# 执行推送（邮件模式）
run_email_mode() {
    if [ -z "$MAIL_TO" ]; then
        log "⚠️  警告: MAIL_TO未设置，跳过邮件发送"
        return 0
    fi
    
    if [ -z "$SMTP_USER" ] || [ -z "$SMTP_PASS" ]; then
        log "⚠️  警告: SMTP凭证未设置，跳过邮件发送"
        return 0
    fi
    
    log "📧 运行邮件模式，发送到: $MAIL_TO"
    
    export SMTP_USER
    export SMTP_PASS
    
    python3 "$PYTHON_SCRIPT" email "$MAIL_TO" 2>&1 | tee -a "$LOG_FILE" || {
        log "❌ 邮件发送失败 (exit=${PIPESTATUS[0]})"
        return 1
    }
    log "✅ 邮件模式完毕"
}

# 显示用法
show_usage() {
    cat << EOF
用法: $0 [选项]

选项:
    console         运行控制台模式（显示新闻到标准输出）
    email          运行邮件模式（发送邮件）
    check          仅检查新闻源可访问性
    full           完整运行（检查+控制台+邮件）
    help           显示此帮助信息

配置:
    设置环境变量或在 ~/.stock-monitor.env 中配置:
    - MAIL_TO       收件人邮箱地址
    - SMTP_USER     SMTP用户名（发件人邮箱）
    - SMTP_PASS     SMTP密码

日志:
    $LOG_DIR/

示例:
    # 仅显示新闻
    $0 console

    # 检查新闻源
    $0 check

    # 发送邮件（需要配置SMTP凭证）
    $0 email

    # 完整运行
    MAIL_TO="user@example.com" $0 full

EOF
}

# 主函数
main() {
    local mode="${1:-console}"
    
    log "🚀 全球新闻推送系统启动"
    log "模式: $mode"
    log "=========================================="
    
    case "$mode" in
        console)
            check_config
            run_console_mode
            ;;
        email)
            check_config
            run_email_mode
            ;;
        check)
            check_config
            check_sources
            ;;
        full)
            check_config
            check_sources
            run_console_mode
            run_email_mode
            ;;
        help)
            show_usage
            exit 0
            ;;
        *)
            log "❌ 未知模式: $mode"
            show_usage
            exit 1
            ;;
    esac
    
    log "=========================================="
    log "✅ 推送系统执行完毕"
    echo ""
}

main "$@"
