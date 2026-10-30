"""E26f 报告公共样式。先调用 configure()，再创建图。"""
from pathlib import Path

STATE_COLOR = {
    'A': '#4C72B0', 'B': '#DD8452', 'C': '#55A868',
    'mixed': '#9974A5', 'io': '#C44E52', 'idle': '#C7C7C7',
}
POLICY_COLOR = {
    'immediate': '#888888', 'slack_only': '#C44E52',
    'quota_fcfs': '#DD8452', 'tfs': '#55A868',
}
VARIANT_COLOR = {'CA': '#6A9FDA', 'CB': '#2C5F9E'}
BANDWIDTH_COLOR = {'demand': '#DD8452', 'actual': '#4C72B0'}
UTIL_BOX_COLOR = '#9DC3E6'
UTIL_MEAN_COLOR = '#C44E52'
HEATMAP = {'cmap': 'viridis', 'vmin': 0, 'vmax': 100}
DPI = 160
FIGSIZE = {
    'bar': (11.8, 6.2), 'timeline': (16.5, 13.5),
    'utilization': (14, 5.3), 'bandwidth': (14.6, 10.8),
    'cdf': (10.2, 6), 'cdf_by_class': (15.5, 4.7),
}


def configure(font_path=None):
    """启用白底样式和中文字体；找不到合适字体时给出明确错误。"""
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import font_manager, ft2font
    import matplotlib.pyplot as plt

    def supports_chinese(path):
        return all(ord(c) in ft2font.FT2Font(str(path)).get_charmap()
                   for c in '实验结果计算等待利用率带宽')

    if font_path is not None:
        path = Path(font_path)
        if not path.is_file() or not supports_chinese(path):
            raise ValueError(f'字体不可用或缺少中文字形：{path}')
    else:
        path = Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
        if not path.is_file() or not supports_chinese(path):
            candidates = ('Arial Unicode MS', 'Noto Sans CJK SC',
                          'Source Han Sans SC', 'Microsoft YaHei', 'SimHei', 'PingFang SC')
            path = next((Path(f.fname) for name in candidates
                         for f in font_manager.fontManager.ttflist
                         if f.name == name and supports_chinese(f.fname)), None)
            if path is None:
                raise RuntimeError('缺少可用中文字体，请通过 configure(font_path=...) 指定。')
    font_manager.fontManager.addfont(str(path))
    family = font_manager.FontProperties(fname=path).get_name()
    plt.rcdefaults()
    plt.rcParams.update({
        'font.family': family, 'font.size': 10,
        'axes.titlesize': 12, 'axes.labelsize': 10,
        'axes.unicode_minus': False, 'figure.facecolor': 'white',
        'axes.facecolor': 'white', 'savefig.facecolor': 'white',
        'legend.fontsize': 9, 'savefig.dpi': DPI,
    })
    return family


def size(kind, policy_count=4, class_count=3):
    """按实际方案/类别数量扩展多面板画布。"""
    if policy_count < 1 or class_count < 1:
        raise ValueError('方案数和类别数必须大于 0')
    w, h = FIGSIZE[kind]
    if kind in ('timeline', 'bandwidth'):
        h *= policy_count / 4
    elif kind == 'cdf_by_class':
        w *= class_count / 3
    return w, h


def save(fig, path):
    """保留指定画布与边距；调用方负责先调整布局并检查图片。"""
    import matplotlib.pyplot as plt
    path = Path(path)
    if path.suffix.lower() != '.png':
        raise ValueError('报告图片必须使用 .png 后缀')
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI, facecolor='white')
    plt.close(fig)
    return path
