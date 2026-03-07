import os

def create_project_structure():
    # 定義資料夾結構
    folders = [
        "core",
        "models",
        "services",
        "utils",
        "tests"
    ]

    # 定義初始檔案與基礎內容
    files = {
        ".env": "TOPSTEP_API_KEY=your_key_here\nDEFAULT_ACCOUNT_ID=\nBASE_URL=https://api.topstepx.com",
        "requirements.txt": "httpx\npydantic\npython-dotenv\npandas",
        ".gitignore": ".env\n__pycache__/\n*.pyc\n.pytest_cache/\nlogs/",
        "core/config.py": "import os\nfrom dotenv import load_dotenv\nload_dotenv()\n\nclass Config:\n    API_KEY = os.getenv('TOPSTEP_API_KEY')\n    BASE_URL = os.getenv('BASE_URL', 'https://api.topstepx.com')\n    ACCOUNT_ID = os.getenv('DEFAULT_ACCOUNT_ID')",
        "core/__init__.py": "",
        "models/__init__.py": "",
        "services/__init__.py": "",
        "utils/__init__.py": "",
    }

    print("🚀 開始初始化 ProjectX 量化交易系統架構...")

    # 建立資料夾
    for folder in folders:
        if not os.path.exists(folder):
            os.makedirs(folder)
            # 建立 __init__.py 讓其成為 Python package
            with open(os.path.join(folder, "__init__.py"), "w") as f:
                pass
            print(f"  - 建立目錄: {folder}/")

    # 建立基礎檔案
    for path, content in files.items():
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"  - 建立檔案: {path}")

    print("\n✅ 初始化完成！")
    print("👉 下一步指引：")
    print("1. 修改 .env 填入你的 API Key。")
    print("2. 執行 'pip install -r requirements.txt' 安裝必要套件。")

if __name__ == "__main__":
    create_project_structure()
