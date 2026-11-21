"""弹丸论破全系列角色名录与原创 Q 版像素小人外观预设（纯标准库，零外部版权图片）。"""

from __future__ import annotations

import re
from typing import Any

_NOISE_WORDS = frozenset({
    "pixel", "pixels", "sprites", "sprite", "8bit", "8-bit", "bit", "icon", "icons",
    "chibi", "mini", "dr1", "dr2", "sdr2", "drv3", "ndrv3", "udg",
    "official", "idle", "walk", "stand", "hd", "sd", "x2", "x3", "x4",
    "gif", "png", "webp", "image", "character", "dot",
    "像素", "点阵", "立绘", "小人", "原版", "官方",
})

_PUNCT_RE = re.compile(r"[\s·・•_.\-—–/\\()（）\[\]【】'\"`~!！?？,，、:：;；]+")


def _p(
    hair_style: str,
    hair_color: str,
    hair_shade: str,
    outfit_style: str,
    outfit_color: str,
    outfit_accent: str,
    outfit_inner: str,
    legs_color: str,
    shoes_color: str,
    *,
    skin: str = "#fde8d8",
    eye_color: str = "#2a2630",
    accessory: str = "none",
    accessory_color: str = "",
) -> dict[str, str]:
    return {
        "hair_style": hair_style,
        "hair_color": hair_color.lower(),
        "hair_shade": hair_shade.lower(),
        "skin": skin.lower(),
        "eye_color": eye_color.lower(),
        "outfit_style": outfit_style,
        "outfit_color": outfit_color.lower(),
        "outfit_accent": outfit_accent.lower(),
        "outfit_inner": outfit_inner.lower(),
        "legs_color": legs_color.lower(),
        "shoes_color": shoes_color.lower(),
        "accessory": accessory,
        "accessory_color": (accessory_color or outfit_accent).lower(),
    }


def _c(
    key: str,
    zh: str,
    en: str,
    series: str,
    aliases: tuple[str, ...],
    preset: dict[str, str],
) -> dict[str, Any]:
    return {
        "key": key,
        "zh": zh,
        "en": en,
        "series": series,
        "aliases": aliases,
        "preset": preset,
    }


DANGANRONPA_CHARACTERS: tuple[dict[str, Any], ...] = (
    # ── DR1（17 位）─────────────────────────────────────────────────────────
    _c(
        "makoto_naegi", "苗木诚", "Makoto Naegi", "DR1",
        ("苗木诚", "苗木誠", "苗木", "苗木 誠", "Makoto Naegi", "Naegi Makoto", "Naegi"),
        _p("spiky", "#8b735b", "#6b5642", "hoodie", "#4b6149", "#c84b4b", "#e8e4dc", "#2c313c", "#9c3d3d", eye_color="#5e6b4c"),
    ),
    _c(
        "kyoko_kirigiri", "雾切响子", "Kyoko Kirigiri", "DR1",
        ("雾切响子", "霧切響子", "雾切", "霧切", "Kirigiri", "Kyoko Kirigiri", "Kirigiri Kyoko"),
        _p("long", "#cfc4de", "#a898c0", "jacket", "#382f48", "#7b5c98", "#f0edf6", "#2b2436", "#3d3349", eye_color="#6a548a", accessory="bow", accessory_color="#2a2335"),
    ),
    _c(
        "byakuya_togami", "十神白夜", "Byakuya Togami", "DR1",
        ("十神白夜", "十神", "Byakuya Togami", "Togami Byakuya", "Togami"),
        _p("short", "#e2cf88", "#bfa85e", "uniform", "#2d3b32", "#b89c52", "#f6f5f0", "#252d28", "#232022", eye_color="#49687a", accessory="glasses", accessory_color="#d8dce6"),
    ),
    _c(
        "toko_fukawa", "腐川冬子", "Toko Fukawa", "DR1",
        ("腐川冬子", "腐川", "灭族者翔", "滅族者翔", "Genocide Jack", "Genocider Syo", "Toko Fukawa", "Fukawa Toko"),
        _p("twin", "#4c3858", "#35263e", "uniform", "#2e3148", "#8c3b5a", "#f0eef4", "#2a2b3d", "#332c38", eye_color="#5e4b6e", accessory="glasses", accessory_color="#a83c5c"),
    ),
    _c(
        "aoi_asahina", "朝日奈葵", "Aoi Asahina", "DR1",
        ("朝日奈葵", "朝日奈", "Aoi Asahina", "Asahina Aoi", "Hina"),
        _p("ponytail", "#6d4630", "#4f3120", "jacket", "#d44a3f", "#f0e8dc", "#ffffff", "#2e3654", "#d44a3f", skin="#e8b896", eye_color="#4a7496"),
    ),
    _c(
        "yasuhiro_hagakure", "叶隐康比吕", "Yasuhiro Hagakure", "DR1",
        ("叶隐康比吕", "葉隱康比呂", "叶隐", "葉隱", "Yasuhiro Hagakure", "Hagakure Yasuhiro"),
        _p("spiky", "#564336", "#3c2d23", "jacket", "#4a6646", "#8a6845", "#e8c4b8", "#333942", "#5c4230", skin="#ecc4a4", eye_color="#6a5440"),
    ),
    _c(
        "sayaka_maizono", "舞园沙耶香", "Sayaka Maizono", "DR1",
        ("舞园沙耶香", "舞園沙耶香", "舞园", "舞園", "Sayaka Maizono", "Maizono Sayaka"),
        _p("long", "#385086", "#263862", "uniform", "#2f3d68", "#d65a7e", "#f7f8fc", "#2a314c", "#4d3834", eye_color="#3e5ea0", accessory="bow", accessory_color="#e8c850"),
    ),
    _c(
        "leon_kuwata", "桑田怜恩", "Leon Kuwata", "DR1",
        ("桑田怜恩", "桑田", "Leon Kuwata", "Kuwata Leon"),
        _p("spiky", "#c84c32", "#96331e", "jacket", "#d8d6dc", "#302d34", "#f4f4f6", "#354158", "#8c3b32", eye_color="#4e6a7c"),
    ),
    _c(
        "chihiro_fujisaki", "不二咲千寻", "Chihiro Fujisaki", "DR1",
        ("不二咲千寻", "不二咲千尋", "千寻", "千尋", "不二咲", "Chihiro Fujisaki", "Fujisaki Chihiro"),
        _p("bob", "#9e7c5a", "#785a3e", "dress", "#3f5d42", "#d8a45c", "#f4efe4", "#364638", "#684836", eye_color="#6d523a"),
    ),
    _c(
        "mondo_owada", "大和田纹土", "Mondo Owada", "DR1",
        ("大和田纹土", "大和田紋土", "大和田", "Mondo Owada", "Owada Mondo", "Mondo Oowada"),
        _p("spiky", "#7a5330", "#593a1f", "jacket", "#24242b", "#d8b44c", "#f4f2ec", "#222228", "#3a2e28", eye_color="#684c38"),
    ),
    _c(
        "kiyotaka_ishimaru", "石丸清多夏", "Kiyotaka Ishimaru", "DR1",
        ("石丸清多夏", "石丸", "Kiyotaka Ishimaru", "Ishimaru Kiyotaka"),
        _p("short", "#26252c", "#16151a", "uniform", "#f0eff4", "#b8363a", "#e4e3ea", "#2a2b32", "#26252c", eye_color="#b8363a"),
    ),
    _c(
        "hifumi_yamada", "山田一二三", "Hifumi Yamada", "DR1",
        ("山田一二三", "山田", "Hifumi Yamada", "Yamada Hifumi"),
        _p("short", "#3a353b", "#242026", "uniform", "#2d2a32", "#5278a8", "#f4f2ee", "#28262e", "#2d2a32", eye_color="#38343c", accessory="glasses", accessory_color="#2d2a32"),
    ),
    _c(
        "celestia_ludenberg", "塞蕾丝蒂娅·卢登贝克", "Celestia Ludenberg", "DR1",
        ("塞蕾丝蒂娅·卢登贝克", "塞蕾丝蒂娅", "塞蕾絲蒂婭", "塞蕾丝", "Celestia Ludenberg", "Celeste", "Taeko Yasuhiro"),
        _p("twin", "#232028", "#141218", "dress", "#26232c", "#b8323e", "#f6f2f4", "#232028", "#201c24", eye_color="#b8323e", accessory="headband", accessory_color="#f4f2f4"),
    ),
    _c(
        "sakura_ogami", "大神樱", "Sakura Ogami", "DR1",
        ("大神樱", "大神櫻", "Sakura Ogami", "Ogami Sakura", "Sakura Oogami"),
        _p("spiky", "#e8e6ec", "#c4c0cc", "uniform", "#2e354d", "#d24c58", "#f4f2f0", "#282e42", "#4a3830", skin="#cfa082", eye_color="#3c3438"),
    ),
    _c(
        "mukuro_ikusaba", "战刃骸", "Mukuro Ikusaba", "DR1",
        ("战刃骸", "戰刃骸", "战刃", "Mukuro Ikusaba", "Ikusaba Mukuro"),
        _p("short", "#2d2c34", "#1b1a20", "uniform", "#2c3140", "#a63846", "#f2f2f6", "#242834", "#242228", eye_color="#5e6878"),
    ),
    _c(
        "junko_enoshima", "江之岛盾子", "Junko Enoshima", "DR1",
        ("江之岛盾子", "江之島盾子", "盾子", "江之岛", "Junko Enoshima", "Enoshima Junko"),
        _p("twin", "#e8b4bc", "#c88e9a", "uniform", "#2c2934", "#c8384c", "#f8f4f6", "#2c2934", "#2a242c", eye_color="#5c84b8", accessory="bow", accessory_color="#2a242c"),
    ),
    _c(
        "alter_ego", "Alter Ego", "Alter Ego", "DR1",
        ("alter ego", "alterego", "人工智能", "不二咲AI"),
        _p("bob", "#9e7c5a", "#785a3e", "hoodie", "#344e3c", "#66cc88", "#d8f4e2", "#26382c", "#2a332d", eye_color="#58b878"),
    ),

    # ── DR2 / SDR2（17 位）──────────────────────────────────────────────────
    _c(
        "hajime_hinata", "日向创", "Hajime Hinata", "DR2",
        ("日向创", "日向創", "日向", "Hajime Hinata", "Hinata Hajime"),
        _p("spiky", "#5a4332", "#3e2d20", "uniform", "#f2f0ec", "#4f6e52", "#ffffff", "#2e323c", "#3c322c", eye_color="#6a7c4e"),
    ),
    _c(
        "izuru_kamukura", "神座出流", "Izuru Kamukura", "DR2",
        ("神座出流", "神座", "Izuru Kamukura", "Kamukura Izuru"),
        _p("long", "#1d1b22", "#100f14", "uniform", "#202028", "#8e2c36", "#e8e8ee", "#1a1a20", "#18171c", eye_color="#b8323e"),
    ),
    _c(
        "chiaki_nanami", "七海千秋", "Chiaki Nanami", "DR2",
        ("七海千秋", "七海", "千秋", "Chiaki Nanami", "Nanami Chiaki", "七海 千秋"),
        _p("bob", "#d8b4bc", "#b68c98", "hoodie", "#4e6152", "#d85a6a", "#f4efe8", "#344038", "#6b4c42", eye_color="#b86b7a", accessory="bow", accessory_color="#e8d8b0"),
    ),
    _c(
        "nagito_komaeda", "狛枝凪斗", "Nagito Komaeda", "DR2",
        ("狛枝凪斗", "狛枝", "凪斗", "Nagito Komaeda", "Komaeda Nagito"),
        _p("spiky", "#ede6de", "#cfc2b8", "jacket", "#4f6a46", "#c84642", "#f2f0ec", "#2d323c", "#58443a", eye_color="#78927c"),
    ),
    _c(
        "akane_owari", "终里赤音", "Akane Owari", "DR2",
        ("终里赤音", "終里赤音", "终里", "赤音", "Akane Owari", "Owari Akane"),
        _p("long", "#543728", "#3a2418", "dress", "#d8443c", "#f4ede4", "#ffffff", "#30364e", "#b83a32", skin="#e6b692", eye_color="#6d4c38"),
    ),
    _c(
        "fuyuhiko_kuzuryu", "九头龙冬彦", "Fuyuhiko Kuzuryu", "DR2",
        ("九头龙冬彦", "九頭龍冬彥", "九头龙", "九頭龍", "Fuyuhiko Kuzuryu", "Kuzuryu Fuyuhiko"),
        _p("short", "#ebd890", "#c8b268", "uniform", "#252630", "#c8a448", "#f4f4f8", "#202128", "#201e24", eye_color="#7a6438"),
    ),
    _c(
        "mahiru_koizumi", "小泉真昼", "Mahiru Koizumi", "DR2",
        ("小泉真昼", "小泉真晝", "小泉", "真昼", "Mahiru Koizumi", "Koizumi Mahiru"),
        _p("bob", "#c44e3c", "#983628", "uniform", "#4d6b4e", "#d65848", "#f6f2ea", "#354836", "#5c3e32", eye_color="#6e7c58"),
    ),
    _c(
        "kazuichi_soda", "左右田和一", "Kazuichi Soda", "DR2",
        ("左右田和一", "左右田", "Kazuichi Soda", "Soda Kazuichi", "Souda Kazuichi"),
        _p("short", "#e86898", "#c24474", "jacket", "#7ca24a", "#e4c848", "#f2f2f2", "#587838", "#38363c", eye_color="#d45888", accessory="headband", accessory_color="#344e3c"),
    ),
    _c(
        "hiyoko_saionji", "西园寺日寄子", "Hiyoko Saionji", "DR2",
        ("西园寺日寄子", "西園寺日寄子", "西园寺", "日寄子", "Hiyoko Saionji", "Saionji Hiyoko"),
        _p("twin", "#efd272", "#cbb052", "dress", "#d97a38", "#d84858", "#f8f0dc", "#c86a30", "#7a4e32", eye_color="#9c7a38", accessory="bow", accessory_color="#e85c68"),
    ),
    _c(
        "sonia_nevermind", "索妮娅·内瓦曼德", "Sonia Nevermind", "DR2",
        ("索妮娅·内瓦曼德", "索妮娅", "索妮婭", "Sonia Nevermind", "Nevermind Sonia"),
        _p("long", "#f2df92", "#d0bc6e", "dress", "#486a5e", "#d44c5e", "#f8f6f0", "#38524a", "#4a3834", eye_color="#5c84b4", accessory="bow", accessory_color="#c83848"),
    ),
    _c(
        "gundham_tanaka", "田中眼蛇梦", "Gundham Tanaka", "DR2",
        ("田中眼蛇梦", "田中眼蛇夢", "田中", "眼蛇梦", "Gundham Tanaka", "Tanaka Gundham"),
        _p("spiky", "#2a2730", "#c8c6d0", "jacket", "#24222a", "#7e3c96", "#423c4e", "#201e24", "#28242c", eye_color="#9c4858"),
    ),
    _c(
        "mikan_tsumiki", "罪木蜜柑", "Mikan Tsumiki", "DR2",
        ("罪木蜜柑", "罪木", "蜜柑", "Mikan Tsumiki", "Tsumiki Mikan"),
        _p("long", "#4a3c54", "#32273a", "dress", "#e6b8c4", "#7b5896", "#f8f4f6", "#f4f2f4", "#5c4a52", eye_color="#786488"),
    ),
    _c(
        "nekomaru_nidai", "贰大猫丸", "Nekomaru Nidai", "DR2",
        ("贰大猫丸", "貳大貓丸", "贰大", "猫丸", "Nekomaru Nidai", "Nidai Nekomaru"),
        _p("spiky", "#2a2830", "#1a181e", "jacket", "#324c7a", "#e4c24a", "#f2f0ea", "#283c60", "#3a3430", skin="#dfb28e", eye_color="#4878b8"),
    ),
    _c(
        "teruteru_hanamura", "花村辉辉", "Teruteru Hanamura", "DR2",
        ("花村辉辉", "花村輝輝", "花村", "Teruteru Hanamura", "Hanamura Teruteru"),
        _p("short", "#4c352c", "#32211a", "uniform", "#f4f2f0", "#c84642", "#ffffff", "#2d2a30", "#3e2c24", eye_color="#483630"),
    ),
    _c(
        "peko_pekoyama", "边谷山佩子", "Peko Pekoyama", "DR2",
        ("边谷山佩子", "邊谷山佩子", "边谷山", "佩子", "Peko Pekoyama", "Pekoyama Peko"),
        _p("twin", "#c8ccd6", "#9da4b4", "uniform", "#2d3548", "#c8424c", "#f4f4f8", "#242a38", "#2e282e", eye_color="#b83c48", accessory="glasses", accessory_color="#c83844"),
    ),
    _c(
        "ibuki_mioda", "澪田唯吹", "Ibuki Mioda", "DR2",
        ("澪田唯吹", "澪田", "唯吹", "Ibuki Mioda", "Mioda Ibuki"),
        _p("twin", "#282632", "#e868a8", "uniform", "#2d385a", "#5ec4e8", "#f4f4f8", "#262c44", "#28242e", eye_color="#c85a98"),
    ),
    _c(
        "ultimate_imposter", "欺诈师", "Ultimate Imposter", "DR2",
        ("欺诈师", "詐欺師", "十神欺诈师", "Ultimate Imposter", "Byakuya Twogami", "Imposter"),
        _p("short", "#e2cf88", "#bfa85e", "uniform", "#f0efe8", "#587096", "#ffffff", "#2c3038", "#2a2628", eye_color="#49687a", accessory="glasses", accessory_color="#d8dce6"),
    ),

    # ── NDRV3（16 位）───────────────────────────────────────────────────────
    _c(
        "kaede_akamatsu", "赤松枫", "Kaede Akamatsu", "V3",
        ("赤松枫", "赤松楓", "赤松", "Kaede Akamatsu", "Akamatsu Kaede"),
        _p("long", "#eed296", "#cbb072", "uniform", "#e8bfd0", "#8e5898", "#f8f4f6", "#4a3e54", "#6e4842", eye_color="#98649e", accessory="bow", accessory_color="#a06ca8"),
    ),
    _c(
        "shuichi_saihara", "最原终一", "Shuichi Saihara", "V3",
        ("最原终一", "最原終一", "最原", "Shuichi Saihara", "Saihara Shuichi"),
        _p("short", "#2d3448", "#1e2334", "uniform", "#252938", "#7884a4", "#e8eaf0", "#202430", "#222026", eye_color="#788868", accessory="headband", accessory_color="#252938"),
    ),
    _c(
        "rantaro_amami", "天海兰太郎", "Rantaro Amami", "V3",
        ("天海兰太郎", "天海蘭太郎", "天海", "Rantaro Amami", "Amami Rantaro"),
        _p("short", "#98b88a", "#749468", "jacket", "#384e3c", "#d8c474", "#f2f0e6", "#2a3430", "#4a3e34", eye_color="#7aa272"),
    ),
    _c(
        "miu_iruma", "入间美兔", "Miu Iruma", "V3",
        ("入间美兔", "入間美兔", "入间", "美兔", "Miu Iruma", "Iruma Miu"),
        _p("long", "#f0b8c8", "#cf92a4", "uniform", "#e87a9a", "#c8a448", "#f8f2f4", "#322834", "#3c2832", eye_color="#6aa4c8", accessory="glasses", accessory_color="#d4b44c"),
    ),
    _c(
        "kokichi_oma", "王马小吉", "Kokichi Oma", "V3",
        ("王马小吉", "王馬小吉", "王马", "王馬", "小吉", "Kokichi Oma", "Kokichi Ouma", "Oma Kokichi", "Ouma Kokichi"),
        _p("spiky", "#2c243a", "#5c3c86", "uniform", "#f2f0f6", "#64448e", "#2d2838", "#e8e6ee", "#2e2834", eye_color="#7a54a8", accessory="headband", accessory_color="#2d2838"),
    ),
    _c(
        "kiibo", "键博", "K1-B0", "V3",
        ("键博", "鍵博", "K1-B0", "K1B0", "Kiibo", "Keebo"),
        _p("spiky", "#d8dce6", "#aeb4c2", "uniform", "#2b303a", "#52c4e8", "#505868", "#232830", "#22262e", eye_color="#4ac0e8"),
    ),
    _c(
        "gonta_gokuhara", "狱原权太", "Gonta Gokuhara", "V3",
        ("狱原权太", "獄原權太", "狱原", "权太", "Gonta Gokuhara", "Gokuhara Gonta"),
        _p("spiky", "#4c5c3e", "#34402a", "uniform", "#4e3e34", "#b89664", "#f0ece4", "#3c3028", "#48362a", skin="#e2b490", eye_color="#984444", accessory="glasses", accessory_color="#b89858"),
    ),
    _c(
        "tsumugi_shirogane", "白银纺", "Tsumugi Shirogane", "V3",
        ("白银纺", "白銀紡", "白银", "Tsumugi Shirogane", "Shirogane Tsumugi"),
        _p("long", "#4874b8", "#30548e", "uniform", "#2c3448", "#c84654", "#f4f4f8", "#242a3a", "#302a32", eye_color="#4874b8", accessory="glasses", accessory_color="#4874b8"),
    ),
    _c(
        "korekiyo_shinguji", "真宫寺是清", "Korekiyo Shinguji", "V3",
        ("真宫寺是清", "真宮寺是清", "真宫寺", "是清", "Korekiyo Shinguji", "Shinguji Korekiyo"),
        _p("long", "#22242a", "#14151a", "uniform", "#3e4e3a", "#b83c44", "#282830", "#2c362a", "#382c26", eye_color="#b8a84c", accessory="headband", accessory_color="#3e4e3a"),
    ),
    _c(
        "tenko_chabashira", "茶柱转子", "Tenko Chabashira", "V3",
        ("茶柱转子", "茶柱轉子", "茶柱", "转子", "Tenko Chabashira", "Chabashira Tenko"),
        _p("twin", "#384e3c", "#263628", "dress", "#4d6b50", "#e6c450", "#f6f4ec", "#344836", "#5c3e32", eye_color="#6a9458", accessory="bow", accessory_color="#7ac478"),
    ),
    _c(
        "kirumi_tojo", "东条斩美", "Kirumi Tojo", "V3",
        ("东条斩美", "東條斬美", "东条", "斩美", "Kirumi Tojo", "Tojo Kirumi", "Kirumi Toujou"),
        _p("bob", "#c4ccd4", "#9ca4ae", "dress", "#24262e", "#e8e8f0", "#f4f4f8", "#1e2026", "#1c1c22", eye_color="#788464", accessory="headband", accessory_color="#f0f0f6"),
    ),
    _c(
        "maki_harukawa", "春川魔姬", "Maki Harukawa", "V3",
        ("春川魔姬", "春川", "魔姬", "Maki Harukawa", "Harukawa Maki"),
        _p("twin", "#34282c", "#221a1c", "uniform", "#a83642", "#26242a", "#f4f0f2", "#242026", "#2a2224", eye_color="#a83642", accessory="bow", accessory_color="#d64c58"),
    ),
    _c(
        "ryoma_hoshi", "星龙马", "Ryoma Hoshi", "V3",
        ("星龙马", "星龍馬", "星", "龙马", "Ryoma Hoshi", "Hoshi Ryoma"),
        _p("short", "#2c3448", "#1c2232", "jacket", "#384664", "#d8843c", "#e8ecf4", "#283246", "#382c26", eye_color="#2a2830", accessory="headband", accessory_color="#384664"),
    ),
    _c(
        "kaito_momota", "百田解斗", "Kaito Momota", "V3",
        ("百田解斗", "百田", "解斗", "Kaito Momota", "Momota Kaito"),
        _p("spiky", "#423254", "#2b2038", "jacket", "#64448e", "#e2c458", "#f2f0f6", "#2d2838", "#383040", skin="#f0c8ab", eye_color="#745498"),
    ),
    _c(
        "himiko_yumeno", "梦野秘密子", "Himiko Yumeno", "V3",
        ("梦野秘密子", "夢野秘密子", "梦野", "秘密子", "Himiko Yumeno", "Yumeno Himiko"),
        _p("bob", "#b84448", "#8e2e32", "uniform", "#2b2c3c", "#b83c48", "#f4f0ec", "#222430", "#4a322c", eye_color="#98484c", accessory="headband", accessory_color="#2b2c3c"),
    ),
    _c(
        "angie_yonaga", "夜长安琪", "Angie Yonaga", "V3",
        ("夜长安琪", "夜長安琪", "夜长", "安琪", "Angie Yonaga", "Yonaga Angie"),
        _p("twin", "#f2ece2", "#d4cab8", "jacket", "#ebd44c", "#e87848", "#ffffff", "#4a78a8", "#8c5c3c", skin="#dfa67e", eye_color="#5c94c8"),
    ),

    # ── UDG & 吉祥物（6 位）─────────────────────────────────────────────────
    _c(
        "komaru_naegi", "苗木困", "Komaru Naegi", "UDG",
        ("苗木困", "困", "Komaru Naegi", "Naegi Komaru"),
        _p("bob", "#7c6552", "#5c4a3a", "uniform", "#2c3c5c", "#d85058", "#f6f6fa", "#26324c", "#583e36", eye_color="#6a7c58"),
    ),
    _c(
        "monaca_towa", "塔和最中", "Monaca Towa", "UDG",
        ("塔和最中", "最中", "Monaca Towa", "Towa Monaca", "Monaca"),
        _p("twin", "#74b878", "#508e54", "dress", "#3e7a4c", "#e8c458", "#f4f8f4", "#32603c", "#4a4038", eye_color="#58a464", accessory="bow", accessory_color="#e8485c"),
    ),
    _c(
        "monokuma", "黑白熊", "Monokuma", "UDG",
        ("黑白熊", "モノクマ", "Monokuma"),
        _p("short", "#242428", "#f4f4f8", "hoodie", "#f4f4f8", "#d83842", "#242428", "#242428", "#242428", skin="#f4f4f8", eye_color="#d83842", accessory="ears", accessory_color="#242428"),
    ),
    _c(
        "monomi", "黑白美", "Monomi", "UDG",
        ("黑白美", "兔美", "兔子", "モノミ", "ウサミ", "Monomi", "Usami"),
        _p("twin", "#f4b8cc", "#f8f4f6", "dress", "#f4a4be", "#ffffff", "#fff6fa", "#f4b8cc", "#e8789a", skin="#f8f4f6", eye_color="#d84868", accessory="ears", accessory_color="#f4b8cc"),
    ),
    _c(
        "monotaro", "黑白熊仔", "Monotaro", "UDG",
        ("黑白熊仔", "Monotaro", "Monokubs"),
        _p("short", "#d84444", "#f4f4f8", "hoodie", "#f4f4f8", "#d84444", "#28282c", "#28282c", "#d84444", skin="#f4f4f8", eye_color="#d84444", accessory="ears", accessory_color="#d84444"),
    ),
    _c(
        "kurokuma", "黑熊", "Kurokuma", "UDG",
        ("黑熊", "白熊", "Kurokuma", "Shirokuma"),
        _p("short", "#242428", "#16161a", "jacket", "#242428", "#d83842", "#404048", "#1e1e22", "#1c1c20", skin="#2c2c32", eye_color="#d83842", accessory="ears", accessory_color="#242428"),
    ),
)


def _normalize_token(text: str) -> str:
    return _PUNCT_RE.sub("", str(text or "").strip()).lower()


def _strip_noise_words(text: str) -> str:
    s = str(text or "").strip().lower()
    # 移除类似 _01 / (1) / x3 的尾部数字标记
    s = re.sub(r"(?:^|[\s_\-])\d{1,3}(?=$|[\s_\-.])", " ", s)
    parts = [p for p in _PUNCT_RE.split(s) if p and p not in _NOISE_WORDS]
    return "".join(parts)


_INDEX_EXACT: dict[str, dict[str, Any]] = {}
_ALIAS_PAIRS: list[tuple[str, dict[str, Any]]] = []
for _entry in DANGANRONPA_CHARACTERS:
    for _raw in (_entry["key"], _entry["zh"], _entry["en"], *_entry["aliases"]):
        _norm = _normalize_token(_raw)
        if _norm:
            _INDEX_EXACT.setdefault(_norm, _entry)
            _ALIAS_PAIRS.append((_norm, _entry))
_ALIAS_PAIRS.sort(key=lambda pair: len(pair[0]), reverse=True)


def _is_cjk_only(s: str) -> bool:
    return bool(s) and all("\u4e00" <= ch <= "\u9fff" for ch in s)


def lookup_character(query: str) -> dict[str, Any] | None:
    """按中/繁/英/日/简称或文件名（含 Pixel/Sprite 等噪声）匹配弹丸论破角色。"""
    q_norm = _normalize_token(query)
    if not q_norm:
        return None
    hit = _INDEX_EXACT.get(q_norm)
    if hit is not None:
        return hit

    q_clean = _strip_noise_words(query)
    if q_clean:
        hit = _INDEX_EXACT.get(q_clean)
        if hit is not None:
            return hit

    # 较长别名包含匹配（优先匹配更长的规范别名，避免单字短名误伤）
    candidates = [c for c in (q_clean, q_norm) if c]
    for cand in candidates:
        for alias_norm, entry in _ALIAS_PAIRS:
            min_len = 2 if _is_cjk_only(alias_norm) else 4
            if len(alias_norm) < min_len:
                continue
            if alias_norm in cand:
                return entry
    return None


def resolve_english_name(name: str) -> str:
    entry = lookup_character(name)
    return str(entry["en"]) if entry else ""


def preset_appearance_for(name: str) -> dict[str, Any] | None:
    entry = lookup_character(name)
    if not entry:
        return None
    out = dict(entry["preset"])
    out["custom_frames"] = {}
    return out


def list_catalog(series: str = "") -> list[dict[str, Any]]:
    want = str(series or "").strip().upper()
    rows: list[dict[str, Any]] = []
    for entry in DANGANRONPA_CHARACTERS:
        if want and want != "ALL" and entry["series"].upper() != want:
            continue
        rows.append({
            "key": entry["key"],
            "zh": entry["zh"],
            "en": entry["en"],
            "series": entry["series"],
        })
    return rows
