import os
import requests

def download_captcha(save_path="captcha.jpg"):
    # 目标 URL
    url = "http://jwzx.hrbust.edu.cn/academic/getCaptcha.do"
    
    # 构建请求头
    headers = {
        "Host": "jwzx.hrbust.edu.cn",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": "http://jwzx.hrbust.edu.cn/academic/common/security/login.jsp",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    
    # 自动管理 Cookie 的会话对象
    session = requests.Session()
    
    # 如果需要复用已有 SessionID，取消下一行注释：
    # session.cookies.set("JSESSIONID", "YOUR_JSESSIONID_HERE")
    
    try:
        # 发送请求获取验证码二进制流
        response = session.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # 确保输出目录存在
        output_dir = os.path.dirname(save_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir)
            
        # 写入文件
        with open(save_path, "wb") as f:
            f.write(response.content)
            
        print(f"验证码保存成功: {save_path}")
        print(f"当前 Session Cookies: {session.cookies.get_dict()}")
        return session
        
    except requests.exceptions.RequestException as e:
        print(f"请求失败: {e}")
        return None

if __name__ == "__main__":
    # 指定保存的文件名或路径
    target_path = "./captcha.jpg"
    download_captcha(target_path)