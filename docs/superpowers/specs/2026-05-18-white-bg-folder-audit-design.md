# White Background Folder Audit Design

## Goal

Create a command-line Python tool that scans `\\192.168.1.6\004.短视频运营中心\01.产品信息\SG` for folders named `01.产品白底图`, checks only direct files in each matching folder, and exports the audit result to an Excel workbook.

## Behavior

- Recursively traverse the root directory.
- Treat each ordinary directory below the scan root as an audited product directory.
- Skip `01.产品白底图` folders themselves and any folders inside them to avoid duplicate product rows.
- If an audited directory has a direct child folder named `01.产品白底图`, count image files only among direct child files of that target folder.
- Ignore images inside subdirectories of the target directory.
- Mark target folders with zero direct image files as `无图片`.
- Mark target folders with one or more direct image files as `有图片`.
- Mark audited directories without a direct `01.产品白底图` child folder as `无白底图`.
- Export every audited directory result.

## Image Extensions

Recognized image extensions are `.jpg`, `.jpeg`, `.png`, `.webp`, `.bmp`, `.gif`, `.tif`, `.tiff`, `.heic`, and `.heif`, case-insensitive.

## Output

The Excel workbook contains these columns:

1. 序号
2. 状态
3. 白底图文件夹路径
4. 上级产品目录
5. 直属图片数量
6. 直属图片文件名
7. 备注

Rows with `无图片` or `无白底图` status are highlighted.

## Error Handling

The tool fails with a clear message if the root directory does not exist or if Excel export dependencies are missing.

## Verification

Use unit tests with temporary folders to verify target-folder discovery, missing target-folder status, direct-only image detection, extension handling, and Excel output creation.
