#!/bin/bash

# 多链稳定币脱锚监控 - 一键管理脚本
# 使用方法: 
#   ./start.sh          # 启动服务
#   ./start.sh stop     # 停止服务
#   ./start.sh restart  # 重启服务
#   ./start.sh status   # 查看状态
#   ./start.sh logs     # 查看日志
#   ./start.sh cli      # 启动 CLI 监控（后台）

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 配置文件路径
PID_FILE="$SCRIPT_DIR/taoli.pid"
CLI_PID_FILE="$SCRIPT_DIR/taoli_cli.pid"
LOG_DIR="$SCRIPT_DIR/logs"
STREAMLIT_LOG="$LOG_DIR/streamlit.log"
CLI_LOG="$LOG_DIR/cli.log"

# 创建日志目录
mkdir -p "$LOG_DIR"

# 获取操作命令（如果没有参数，显示菜单）
ACTION="${1:-menu}"

# ========== 辅助函数 ==========

check_python() {
    if ! command -v python3 &> /dev/null; then
        echo -e "${RED}❌ 错误: 未找到 python3，请先安装 Python 3${NC}"
        exit 1
    fi
    echo "$(which python3)"
}

get_pid() {
    local pid_file="$1"
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file" 2>/dev/null)
        if [ -n "$pid" ] && ps -p "$pid" > /dev/null 2>&1; then
            echo "$pid"
        else
            rm -f "$pid_file"
            echo ""
        fi
    fi
    echo ""
}

find_streamlit_pid() {
    pgrep -f "streamlit run taoli.py" | head -n 1 || echo ""
}

find_cli_pid() {
    pgrep -f "python.*taoli.py cli" | head -n 1 || echo ""
}

stop_process() {
    local pid="$1"
    local name="$2"
    if [ -n "$pid" ] && ps -p "$pid" > /dev/null 2>&1; then
        echo -e "${YELLOW}正在停止 $name (PID: $pid)...${NC}"
        kill -TERM "$pid" 2>/dev/null || true
        
        # 等待进程结束（最多等待 10 秒）
        for i in {1..10}; do
            if ! ps -p "$pid" > /dev/null 2>&1; then
                break
            fi
            sleep 1
        done
        
        # 如果还在运行，强制杀死
        if ps -p "$pid" > /dev/null 2>&1; then
            echo -e "${YELLOW}强制停止进程...${NC}"
            kill -9 "$pid" 2>/dev/null || true
        fi
        return 0
    fi
    return 1
}

# ========== 启动服务 ==========

start_service() {
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}多链稳定币脱锚监控 - 启动服务${NC}"
    echo -e "${GREEN}========================================${NC}"
    
    # 检查 Python
    PYTHON_CMD=$(check_python)
    echo -e "${GREEN}✓${NC} Python: $PYTHON_CMD"
    
    # 检查是否已经运行
    STREAMLIT_PID=$(get_pid "$PID_FILE")
    if [ -z "$STREAMLIT_PID" ]; then
        STREAMLIT_PID=$(find_streamlit_pid)
    fi
    
    if [ -n "$STREAMLIT_PID" ]; then
        echo -e "${YELLOW}⚠️  服务已在运行 (PID: $STREAMLIT_PID)${NC}"
        echo -e "${YELLOW}   如需重启，请运行: ./start.sh restart${NC}"
        exit 1
    fi
    
    # 检查端口 8501 是否被占用
    if lsof -Pi :8501 -sTCP:LISTEN -t >/dev/null 2>&1 ; then
        echo -e "${YELLOW}⚠️  端口 8501 已被占用，正在清理...${NC}"
        lsof -ti:8501 | xargs kill -9 2>/dev/null || true
        sleep 2
    fi
    
    # 检查依赖
    echo -e "${GREEN}检查依赖...${NC}"
    $PYTHON_CMD -m pip show streamlit > /dev/null 2>&1 || {
        echo -e "${YELLOW}⚠️  正在安装依赖...${NC}"
        $PYTHON_CMD -m pip install requests pandas streamlit plotly --quiet
    }
    
    # 启动 Streamlit 服务（后台运行）
    echo -e "${GREEN}启动 Streamlit 服务...${NC}"
    nohup streamlit run taoli.py --server.port 8501 --server.address 0.0.0.0 --server.headless true > "$STREAMLIT_LOG" 2>&1 &
    STREAMLIT_PID=$!
    
    # 等待一下，检查是否成功启动
    sleep 3
    if ps -p "$STREAMLIT_PID" > /dev/null 2>&1; then
        echo "$STREAMLIT_PID" > "$PID_FILE"
        echo -e "${GREEN}✓${NC} Streamlit 服务已启动 (PID: $STREAMLIT_PID)"
        echo -e "${GREEN}✓${NC} 访问地址: http://localhost:8501"
        echo -e "${GREEN}✓${NC} 日志文件: $STREAMLIT_LOG"
        echo ""
        echo -e "${GREEN}========================================${NC}"
        echo -e "${GREEN}服务运行中...${NC}"
        echo -e "${GREEN}停止服务: ./start.sh stop${NC}"
        echo -e "${GREEN}查看日志: ./start.sh logs${NC}"
        echo -e "${GREEN}========================================${NC}"
    else
        echo -e "${RED}❌ 启动失败，请查看日志: $STREAMLIT_LOG${NC}"
        exit 1
    fi
}

# ========== 停止服务 ==========

stop_service() {
    echo -e "${YELLOW}========================================${NC}"
    echo -e "${YELLOW}多链稳定币脱锚监控 - 停止服务${NC}"
    echo -e "${YELLOW}========================================${NC}"
    
    STOPPED=0
    
    # 停止 Streamlit 服务
    STREAMLIT_PID=$(get_pid "$PID_FILE")
    if [ -z "$STREAMLIT_PID" ]; then
        STREAMLIT_PID=$(find_streamlit_pid)
    fi
    
    if [ -n "$STREAMLIT_PID" ]; then
        stop_process "$STREAMLIT_PID" "Streamlit 服务"
        STOPPED=1
        rm -f "$PID_FILE"
    fi
    
    # 通过端口查找并停止（备用方法）
    if lsof -Pi :8501 -sTCP:LISTEN -t >/dev/null 2>&1 ; then
        echo -e "${YELLOW}发现端口 8501 仍被占用，正在停止...${NC}"
        lsof -ti:8501 | xargs kill -9 2>/dev/null || true
        STOPPED=1
    fi
    
    if [ "$STOPPED" -eq 1 ]; then
        echo -e "${GREEN}✓${NC} 服务已停止"
    else
        echo -e "${YELLOW}✓${NC} 服务未运行"
    fi
    
    echo -e "${GREEN}========================================${NC}"
}

# ========== 重启服务 ==========

restart_service() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}多链稳定币脱锚监控 - 重启服务${NC}"
    echo -e "${BLUE}========================================${NC}"
    
    stop_service
    sleep 2
    start_service
}

# ========== 查看状态 ==========

show_status() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}多链稳定币脱锚监控 - 服务状态${NC}"
    echo -e "${BLUE}========================================${NC}"
    
    # Streamlit 服务状态
    STREAMLIT_PID=$(get_pid "$PID_FILE")
    if [ -z "$STREAMLIT_PID" ]; then
        STREAMLIT_PID=$(find_streamlit_pid)
    fi
    
    if [ -n "$STREAMLIT_PID" ]; then
        echo -e "${GREEN}✓${NC} Streamlit 服务: ${GREEN}运行中${NC} (PID: $STREAMLIT_PID)"
        
        # 检查端口
        if lsof -Pi :8501 -sTCP:LISTEN -t >/dev/null 2>&1 ; then
            echo -e "${GREEN}✓${NC} 端口 8501: ${GREEN}已监听${NC}"
            echo -e "${GREEN}✓${NC} 访问地址: http://localhost:8501"
        else
            echo -e "${YELLOW}⚠️  端口 8501: 未监听${NC}"
        fi
    else
        echo -e "${RED}✗${NC} Streamlit 服务: ${RED}未运行${NC}"
    fi
    
    # CLI 监控状态
    CLI_PID=$(get_pid "$CLI_PID_FILE")
    if [ -z "$CLI_PID" ]; then
        CLI_PID=$(find_cli_pid)
    fi
    
    if [ -n "$CLI_PID" ]; then
        echo -e "${GREEN}✓${NC} CLI 监控: ${GREEN}运行中${NC} (PID: $CLI_PID)"
    else
        echo -e "${YELLOW}○${NC} CLI 监控: ${YELLOW}未运行${NC}"
    fi
    
    echo ""
    echo -e "${BLUE}日志文件:${NC}"
    echo -e "  Streamlit: $STREAMLIT_LOG"
    echo -e "  CLI: $CLI_LOG"
    echo ""
    echo -e "${BLUE}========================================${NC}"
}

# ========== 查看日志 ==========

show_logs() {
    local log_type="${2:-streamlit}"
    
    if [ "$log_type" = "cli" ]; then
        if [ -f "$CLI_LOG" ]; then
            echo -e "${BLUE}查看 CLI 日志 (按 Ctrl+C 退出)...${NC}"
            tail -f "$CLI_LOG"
        else
            echo -e "${YELLOW}CLI 日志文件不存在: $CLI_LOG${NC}"
        fi
    else
        if [ -f "$STREAMLIT_LOG" ]; then
            echo -e "${BLUE}查看 Streamlit 日志 (按 Ctrl+C 退出)...${NC}"
            tail -f "$STREAMLIT_LOG"
        else
            echo -e "${YELLOW}Streamlit 日志文件不存在: $STREAMLIT_LOG${NC}"
        fi
    fi
}

# ========== 启动 CLI 监控 ==========

start_cli() {
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}多链稳定币脱锚监控 - 启动 CLI 监控${NC}"
    echo -e "${GREEN}========================================${NC}"
    
    # 检查 Python
    PYTHON_CMD=$(check_python)
    echo -e "${GREEN}✓${NC} Python: $PYTHON_CMD"
    
    # 检查是否已经运行
    CLI_PID=$(get_pid "$CLI_PID_FILE")
    if [ -z "$CLI_PID" ]; then
        CLI_PID=$(find_cli_pid)
    fi
    
    if [ -n "$CLI_PID" ]; then
        echo -e "${YELLOW}⚠️  CLI 监控已在运行 (PID: $CLI_PID)${NC}"
        exit 1
    fi
    
    # 启动 CLI 监控（后台运行）
    echo -e "${GREEN}启动 CLI 监控...${NC}"
    nohup $PYTHON_CMD taoli.py cli > "$CLI_LOG" 2>&1 &
    CLI_PID=$!
    
    # 等待一下，检查是否成功启动
    sleep 2
    if ps -p "$CLI_PID" > /dev/null 2>&1; then
        echo "$CLI_PID" > "$CLI_PID_FILE"
        echo -e "${GREEN}✓${NC} CLI 监控已启动 (PID: $CLI_PID)"
        echo -e "${GREEN}✓${NC} 日志文件: $CLI_LOG"
        echo ""
        echo -e "${GREEN}========================================${NC}"
        echo -e "${GREEN}CLI 监控运行中...${NC}"
        echo -e "${GREEN}查看日志: ./start.sh logs cli${NC}"
        echo -e "${GREEN}========================================${NC}"
    else
        echo -e "${RED}❌ 启动失败，请查看日志: $CLI_LOG${NC}"
        exit 1
    fi
}

# ========== 停止 CLI 监控 ==========

stop_cli() {
    CLI_PID=$(get_pid "$CLI_PID_FILE")
    if [ -z "$CLI_PID" ]; then
        CLI_PID=$(find_cli_pid)
    fi
    
    if [ -n "$CLI_PID" ]; then
        stop_process "$CLI_PID" "CLI 监控"
        rm -f "$CLI_PID_FILE"
        echo -e "${GREEN}✓${NC} CLI 监控已停止"
    else
        echo -e "${YELLOW}✓${NC} CLI 监控未运行"
    fi
}

# ========== 显示菜单 ==========

show_menu() {
    # 清屏（可选）
    # clear
    
    echo -e "${BLUE}╔════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║${NC}  多链稳定币脱锚监控 - 管理菜单      ${BLUE}║${NC}"
    echo -e "${BLUE}╠════════════════════════════════════════╣${NC}"
    echo ""
    
    # 显示当前状态
    STREAMLIT_PID=$(get_pid "$PID_FILE")
    if [ -z "$STREAMLIT_PID" ]; then
        STREAMLIT_PID=$(find_streamlit_pid)
    fi
    
    CLI_PID=$(get_pid "$CLI_PID_FILE")
    if [ -z "$CLI_PID" ]; then
        CLI_PID=$(find_cli_pid)
    fi
    
    echo -e "${BLUE}当前状态:${NC}"
    if [ -n "$STREAMLIT_PID" ]; then
        echo -e "  ${GREEN}✓${NC} Streamlit 服务: ${GREEN}运行中${NC} (PID: $STREAMLIT_PID)"
    else
        echo -e "  ${RED}✗${NC} Streamlit 服务: ${RED}未运行${NC}"
    fi
    
    if [ -n "$CLI_PID" ]; then
        echo -e "  ${GREEN}✓${NC} CLI 监控: ${GREEN}运行中${NC} (PID: $CLI_PID)"
    else
        echo -e "  ${YELLOW}○${NC} CLI 监控: ${YELLOW}未运行${NC}"
    fi
    
    echo ""
    echo -e "${BLUE}╠════════════════════════════════════════╣${NC}"
    echo -e "${BLUE}║${NC}  ${GREEN}Streamlit 服务管理${NC}                    ${BLUE}║${NC}"
    echo -e "${BLUE}╠════════════════════════════════════════╣${NC}"
    echo -e "${BLUE}║${NC}  ${YELLOW}1${NC}. 启动 Streamlit 服务                ${BLUE}║${NC}"
    echo -e "${BLUE}║${NC}  ${YELLOW}2${NC}. 停止 Streamlit 服务                ${BLUE}║${NC}"
    echo -e "${BLUE}║${NC}  ${YELLOW}3${NC}. 重启 Streamlit 服务                ${BLUE}║${NC}"
    echo -e "${BLUE}║${NC}  ${YELLOW}4${NC}. 查看服务状态                        ${BLUE}║${NC}"
    echo -e "${BLUE}║${NC}  ${YELLOW}5${NC}. 查看 Streamlit 日志                ${BLUE}║${NC}"
    echo ""
    echo -e "${BLUE}╠════════════════════════════════════════╣${NC}"
    echo -e "${BLUE}║${NC}  ${GREEN}CLI 监控管理${NC}                          ${BLUE}║${NC}"
    echo -e "${BLUE}╠════════════════════════════════════════╣${NC}"
    echo -e "${BLUE}║${NC}  ${YELLOW}6${NC}. 启动 CLI 监控                      ${BLUE}║${NC}"
    echo -e "${BLUE}║${NC}  ${YELLOW}7${NC}. 停止 CLI 监控                      ${BLUE}║${NC}"
    echo -e "${BLUE}║${NC}  ${YELLOW}8${NC}. 查看 CLI 日志                      ${BLUE}║${NC}"
    echo ""
    echo -e "${BLUE}╠════════════════════════════════════════╣${NC}"
    echo -e "${BLUE}║${NC}  ${GREEN}其他${NC}                                  ${BLUE}║${NC}"
    echo -e "${BLUE}╠════════════════════════════════════════╣${NC}"
    echo -e "${BLUE}║${NC}  ${YELLOW}9${NC}. 显示帮助信息                        ${BLUE}║${NC}"
    echo -e "${BLUE}║${NC}  ${YELLOW}0${NC}. 退出                              ${BLUE}║${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${GREEN}请选择操作 [0-9]:${NC} "
    read -r choice
    
    case "$choice" in
        1)
            start_service
            echo ""
            echo -e "${YELLOW}按 Enter 键返回菜单...${NC}"
            read -r
            show_menu
            ;;
        2)
            stop_service
            echo ""
            echo -e "${YELLOW}按 Enter 键返回菜单...${NC}"
            read -r
            show_menu
            ;;
        3)
            restart_service
            echo ""
            echo -e "${YELLOW}按 Enter 键返回菜单...${NC}"
            read -r
            show_menu
            ;;
        4)
            show_status
            echo ""
            echo -e "${YELLOW}按 Enter 键返回菜单...${NC}"
            read -r
            show_menu
            ;;
        5)
            echo -e "${YELLOW}提示: 按 Ctrl+C 退出日志查看，返回菜单${NC}"
            sleep 2
            show_logs streamlit || true
            echo ""
            echo -e "${YELLOW}按 Enter 键返回菜单...${NC}"
            read -r
            show_menu
            ;;
        6)
            start_cli
            echo ""
            echo -e "${YELLOW}按 Enter 键返回菜单...${NC}"
            read -r
            show_menu
            ;;
        7)
            stop_cli
            echo ""
            echo -e "${YELLOW}按 Enter 键返回菜单...${NC}"
            read -r
            show_menu
            ;;
        8)
            echo -e "${YELLOW}提示: 按 Ctrl+C 退出日志查看，返回菜单${NC}"
            sleep 2
            show_logs cli || true
            echo ""
            echo -e "${YELLOW}按 Enter 键返回菜单...${NC}"
            read -r
            show_menu
            ;;
        9)
            show_help
            echo ""
            echo -e "${YELLOW}按 Enter 键返回菜单...${NC}"
            read -r
            show_menu
            ;;
        0)
            echo -e "${GREEN}退出${NC}"
            exit 0
            ;;
        *)
            echo -e "${RED}❌ 无效选择，请重新输入${NC}"
            sleep 1
            show_menu
            ;;
    esac
}

# ========== 显示帮助 ==========

show_help() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}多链稳定币脱锚监控 - 使用说明${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""
    echo -e "${GREEN}命令行使用方法:${NC}"
    echo -e "  ${YELLOW}./start.sh${NC}              # 显示交互菜单"
    echo -e "  ${YELLOW}./start.sh start${NC}        # 启动 Streamlit 服务"
    echo -e "  ${YELLOW}./start.sh stop${NC}         # 停止 Streamlit 服务"
    echo -e "  ${YELLOW}./start.sh restart${NC}     # 重启 Streamlit 服务"
    echo -e "  ${YELLOW}./start.sh status${NC}       # 查看服务状态"
    echo -e "  ${YELLOW}./start.sh logs${NC}         # 查看 Streamlit 日志（实时）"
    echo -e "  ${YELLOW}./start.sh logs cli${NC}     # 查看 CLI 监控日志（实时）"
    echo -e "  ${YELLOW}./start.sh cli${NC}          # 启动 CLI 监控（后台）"
    echo -e "  ${YELLOW}./start.sh cli-stop${NC}     # 停止 CLI 监控"
    echo -e "  ${YELLOW}./start.sh help${NC}         # 显示此帮助信息"
    echo ""
    echo -e "${GREEN}默认访问地址:${NC} http://localhost:8501"
    echo ""
}

# ========== 主逻辑 ==========

case "$ACTION" in
    menu)
        show_menu
        ;;
    start)
        start_service
        ;;
    stop)
        stop_service
        ;;
    restart)
        restart_service
        ;;
    status)
        show_status
        ;;
    logs)
        show_logs "$@"
        ;;
    cli)
        start_cli
        ;;
    cli-stop)
        stop_cli
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        echo -e "${RED}❌ 未知操作: $ACTION${NC}"
        echo ""
        show_help
        exit 1
        ;;
esac

