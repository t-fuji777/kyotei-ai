# -*- coding: utf-8 -*-
"""scripts/fetch_result.parse_before の欠場の印(is-miss)の読み方。通信なし(合成 HTML)。

欠場の印は tbody の class 属性の中の単語 is-miss だけに合わせる。部分一致だと is-missing のような別名の
class が付いた艇(展示タイムあり)まで欠場と誤判定し、出走する艇を買い目から外して再予測してしまう。

実行: PYTHONUTF8=1 python -X utf8 tests/test_fetch_before.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fetch_result as FR  # noqa: E402


def tbody(lane, cls, ex):
    """直前情報ページの艇の行のまね(体重・チルトなど展示タイムと紛らわしい数値も入れる)。"""
    ex_td = f"<td>{ex:.2f}</td>" if ex is not None else "<td></td>"
    return (f'<tbody class="{cls}"><tr><td class="is-fs14 is-boatColor{lane}">{lane}</td>'
            f'<td>選手{lane}</td><td>52.0kg</td><td>-0.5</td>{ex_td}<td>.12</td></tr></tbody>')


def page(rows):
    return ('<html><body><table class="is-w748">' + "".join(rows) + "</table>"
            '<table class="is-w238"><tbody></tbody></table>'
            '<div class="weather1_bodyUnit is-windDirection"><span class="weather1_bodyUnitLabelTitle">風速</span> '
            '<span class="weather1_bodyUnitLabelData">3</span></div>'
            '<div><span class="weather1_bodyUnitLabelTitle">波高</span> <span class="weather1_bodyUnitLabelData">2</span></div>'
            "</body></html>")


def main():
    assert FR.ABSENT_RULE is True
    # 2号艇が欠場(is-miss。展示なし)、6号艇には is-missing という別名の class(展示あり)
    html = page([tbody(1, "is-fs12", 6.80), tbody(2, "is-fs12 is-miss", None), tbody(3, "is-fs12", 6.85),
                 tbody(4, "is-fs12", 6.90), tbody(5, "is-fs12", 6.88), tbody(6, "is-missing is-fs12", 6.91)])
    bi = FR.parse_before(html)
    assert bi["absent"] == [2], bi
    assert sorted(bi["ex"]) == [1, 3, 4, 5, 6] and bi["ex"][6] == 6.91, bi["ex"]
    assert FR.absent_lanes(bi) == [2] and FR.ex_complete(bi) is True
    assert bi["wind"] == 3 and bi["wave"] == 2
    print("ok   is-miss の艇だけ欠場。is-missing の艇は出走(展示あり)で、5艇そろいとして再予測に進める")

    # 印の付き方: 先頭・末尾・単独・シングルクォート。is-miss2 / foo-is-miss は欠場でない
    for cls, want in (("is-miss", True), ("is-miss is-fs12", True), ("is-fs12 is-miss", True), ("is-fs12 is-miss is-boat", True),
                      ("is-missing", False), ("is-miss2", False), ("foo-is-miss", False), ("is-fs12", False), ("", False)):
        bi = FR.parse_before(page([tbody(1, "is-fs12", 6.80), tbody(2, cls, None)]))
        assert (bi["absent"] == [2]) is want, (cls, bi["absent"])
        sq = page([tbody(1, "is-fs12", 6.80), tbody(2, cls, None)]).replace(f'class="{cls}"', f"class='{cls}'")
        assert (FR.parse_before(sq)["absent"] == [2]) is want, ("single quote", cls)
    print("ok   class の中の単語 is-miss だけに合う(is-missing / is-miss2 / foo-is-miss には合わない。' で囲んでも同じ)")

    # 展示が出ていない艇に印が無ければ欠場ではない(ページがまだ埋まりきっていない状態) → 6艇そろわず再予測しない
    bi = FR.parse_before(page([tbody(1, "is-fs12", 6.80), tbody(2, "is-fs12", None), tbody(3, "is-fs12", 6.85),
                               tbody(4, "is-fs12", 6.90), tbody(5, "is-fs12", 6.88), tbody(6, "is-fs12", 6.91)]))
    assert bi["absent"] == [] and sorted(bi["ex"]) == [1, 3, 4, 5, 6] and FR.ex_complete(bi) is False
    # 展示が4艇未満なら、欠場の印があっても再予測しない(MIN_EX_BOATS)
    bi = FR.parse_before(page([tbody(1, "is-fs12", 6.80), tbody(2, "is-miss", None), tbody(3, "is-miss", None),
                               tbody(4, "is-fs12", 6.90), tbody(5, "is-fs12", 6.88), tbody(6, "is-miss", None)]))
    assert bi["absent"] == [2, 3, 6] and FR.ex_complete(bi) is False
    # ABSENT_RULE=False なら印は読むが使わない(6艇そろいだけ)
    FR.ABSENT_RULE = False
    try:
        bi = FR.parse_before(html)
        assert bi["absent"] == [2] and FR.absent_lanes(bi) == [] and FR.ex_complete(bi) is False
    finally:
        FR.ABSENT_RULE = True
    print("ok   印の無い展示なしは欠場でない / 展示4艇未満は再予測しない / ABSENT_RULE=False なら6艇そろいだけ")
    print("ALL OK")


if __name__ == "__main__":
    main()
