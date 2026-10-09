"""测试后端是否能正常启动"""
from app import app

if __name__ == '__main__':
    print("测试启动 Flask 应用...")
    print("访问 http://localhost:9000")
    app.run(host='0.0.0.0', port=9000, debug=True)
