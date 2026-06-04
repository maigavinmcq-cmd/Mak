provider_quickstart

文件说明:
- common.py: 公共工具函数
- jimmy_provider.py: Jimmy API 示例，包含直传 image_url 和 OSS 换链两个版本
- oss_uploader.py: OSS 上传并生成签名链接
- dyuapi_provider.py: DYUAPI 示例
- xintian_provider.py: Xintian API 示例
- demo_main.py: 统一调用示例
- provider_router.py: 统一入口，按 provider 名称路由调用

依赖:
- requests

注意:
1. demo_main.py 默认未执行任何 provider，请手动取消注释。
2. Jimmy 支持两种模式:
   - jimmy: 外部直接提供 image_url
   - jimmy_oss: 提供本地 image_path，先上传 OSS 再换成 image_url
3. dyuapi / xintian 按本地图片 multipart/form-data 上传。
4. 推荐新工程优先使用 provider_router.py 的 run_provider(provider, config)。
