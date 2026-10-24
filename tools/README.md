# tools/ 目录

把 **adb.exe** 和 **fastboot.exe**（以及配套 DLL）放到本目录，程序会自动使用。

下载地址（platform-tools 官方包，解压后把里面所有文件拷进来）：

    https://developer.android.com/tools/releases/platform-tools

也可以在程序内：`工具 → 设置 adb/fastboot 路径…` 手动指定。

Windows 常见问题：若提示缺少 DLL，请把 platform-tools 解压出的 `AdbWinApi.dll`、`AdbWinUsbApi.dll` 一并放入本目录。
