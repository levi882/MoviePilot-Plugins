# MoviePilot 插件

- [FEL 自动重建（V3，实验性）](plugins.v3/felplayback/README.md)：沿用原电影库，重建 P7 FEL；需独立处理服务。
- 现有企业微信插件说明见下文，原有插件与索引保留。

---

## 插件介绍
自动检测动态公网IP并应用到企业微信应用可信IP  
微信通知代理地址记得改回官方地址：https://qyapi.weixin.qq.com/ 并重启MP
docker用户使用PW版  
Windows用户推荐使用普通版  
[手动提取cookie流程](#手动获取cookie流程)  
[多WAN多IP注意事项](#多wan多公网ip环境答疑)  
[无法识别二维码登录](#无法通过识别图片二维码登录)  

## 登录流程  
默认开启CookieCloud（简称CC），从MP官方的CC配置读取cookie  
当插件检测到登录cookie失效时，会先从CC获取企业微信cookie尝试登录(防止顶掉其他地方的登录)  
自动登录开关默认关闭，发送 #登录企业微信 至MP应用则可以唤起一次登录操作。如果需要验证手机，把验证码按照格式 #123456 发送到MP应用  
Cookie失效通知仅在关闭自动登录时生效，会在cookie失效后定期发送通知到MP应用提醒登录  
若开启自动登录，Cookie失效后会自动循环登录流程。若未及时登录会导致MP应用聊天框被塞满二维码。不推荐开启  
登录二维码通过MP服务器发送到企业微信MP应用端，点击图片打开二维码长按识别登录即可  
偶尔会出现二维码获取失败的情况，等待当前登录超时后重新登录即可。  
![登录示例](https://github.com/user-attachments/assets/23a54602-36bb-4dd8-aa83-2a1136a9b72d)
![验证码流程](https://github.com/user-attachments/assets/9ba15980-8c91-46bf-9eb7-01fe5ff196d8)
![扫码登录](https://github.com/user-attachments/assets/702d75c9-082f-432d-a938-465963a3dbcd)





特殊情况下，比如登录缓存失效了，而又没有及时在企业微信MP应用扫描登录，刚好动态IP又刷新，MP应用就无法获取最新登录二维码  
此时可打开MP网页端，打开插件扫描二维码登录即可。  
![image](https://github.com/user-attachments/assets/a9638858-fac8-441b-920f-4b8255bedfdc)  
## 手动获取cookie流程（非必要步骤）
使用浏览器cookie插件(([Cookie Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm))导出HeaderString格式的cookie,
登录企业微信后按下图所示导出cookie  
如果只使用手动抓取填写的cookie，后续如果用浏览器扫码登录企业微信，则上次抓取的cookie会失效，需重新抓取  
![微信截图_20240605203353](https://github.com/suraxiuxiu/MoviePilot-Plugins/assets/41566282/6f107697-5e96-4cef-821e-bb3df5b6e7a9)

## 多WAN、多公网IP环境答疑
插件目前不考虑适配这种环境，插件端无论请求多少次IP检测网站，都无法保证会返回两个宽带的IP。  
因为这是路由端负责的，可能请求一百次也是分配A宽带去访问并返回A宽带的IP，所以在路由端设置可以很容易解决这个问题。  
可以在路由器多WAN设置静态路由分配，把企业微信网址和下面的ip检测网址都指定同一条宽带出口。  
保证插件检测到的公网IP、企业微信应用的可信IP、MP请求企业微信应用时走的IP是同一个即可解决。  
插件所使用的IP检测网址：
```text
https://myip.ipip.net
https://ddns.oray.com/checkip
https://ip.3322.net
https://4.ipw.cn
```

## 无法通过识别图片二维码登录 
如果微信识别登录二维码图片提示跳转企业微信，跳转后也识别不了，提示无法通过这种方式登录一类的。
也许是账号风控了，有群友尝试过可以转移企业微信给自己小号或者用新号创建企业微信就能识别扫码登录。
然后把自己的主号添加到同一个企业来使用MP应用，具体自己尝试。

