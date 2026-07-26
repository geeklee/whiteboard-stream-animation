import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np


SCRIPT = Path(__file__).with_name("stream_render.py")
SPEC = importlib.util.spec_from_file_location("stream_render_under_test", SCRIPT)
stream_render = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = stream_render
SPEC.loader.exec_module(stream_render)


class InkRevealTests(unittest.TestCase):
    def test_phase_defaults_prioritize_line_art_two_to_one(self):
        cfg = stream_render.Config()

        self.assertEqual((cfg.ink_weight, cfg.color_weight), (2, 1))

    def test_groups_subject_text_and_local_contours_in_that_order(self):
        # 三个足够大的区域，都不会被当碎片合并
        active = np.zeros((30, 30), dtype=bool)
        active[0:8, 0:8] = True       # compact main subject (64 格)
        active[10:14, 0:16] = True    # dense horizontal text-like region (64 格)
        active[20:28, 20:28] = True   # local contour (64 格)

        groups = stream_render.classify_stroke_groups(active)

        self.assertEqual([kind for kind, _ in groups], ["subject", "text", "contour"])

    def test_small_fragments_merge_into_nearest_large_region(self):
        # 一个大主体 + 两个 1 格碎片。碎片应被并入最近的大区域，
        # 不再作为独立 stream 穿插，避免打断大块文字的连续绘制。
        active = np.zeros((30, 30), dtype=bool)
        active[10:20, 10:20] = True   # 主体 100 格
        active[2, 3] = True           # 碎片1（左上角）
        active[5, 25] = True          # 碎片2（右上角）

        groups = stream_render.classify_stroke_groups(active)

        # 合并后只剩 1 个组（主体），碎片被吸收
        self.assertEqual(len(groups), 1)
        merged_cells = groups[0][1]
        # 所有原始墨迹格都在（不丢弃墨迹）
        self.assertEqual(len(merged_cells), 100 + 1 + 1)

    def test_thin_baseline_bridge_splits_into_complete_visual_regions(self):
        # 两个主体由一根细基线接上；不能把它们当成一条大墨流来回穿插。
        active = np.zeros((24, 48), dtype=bool)
        active[2:14, 2:14] = True
        active[2:14, 30:42] = True
        active[8, 14:30] = True
        labels, count = stream_render._label_components(active)
        connected = stream_render._component_cells(labels, 1)

        regions = stream_render._split_bridge_connected_component(connected)

        self.assertEqual(len(regions), 2)
        self.assertTrue(all(len(region) >= 20 for region in regions))
        self.assertLessEqual(max(col for _, col in regions[0]), 21)
        self.assertGreaterEqual(min(col for _, col in regions[1]), 22)

    def test_overlapping_label_parts_are_locked_into_one_region(self):
        border = [(row, col) for row in range(0, 5) for col in range(0, 12)]
        letters = [(row, col) for row in range(1, 4) for col in range(2, 10)]
        distant = [(row, col) for row in range(12, 18) for col in range(24, 30)]

        regions = stream_render._group_adjacent_stroke_groups([
            ("contour", border), ("text", letters), ("contour", distant),
        ])

        self.assertEqual([len(region) for region in regions], [2, 1])

    def test_defaults_use_fine_high_frequency_cursor_motion(self):
        cfg = stream_render.Config()

        self.assertEqual(cfg.fps, 60)
        self.assertEqual(cfg.grid_edge, 10)
        self.assertEqual(cfg.sample_step, 2)

    def test_non_adjacent_cells_start_a_new_pen_down_segment(self):
        renderer = object.__new__(stream_render.StreamBoardRenderer)
        renderer.cfg = stream_render.Config()

        samples, pen_lifts, _sample_cell_index = renderer._build_stroke_samples(
            [(0, 0), (0, 1), (4, 4)]
        )

        self.assertEqual(len(pen_lifts), 1)
        self.assertIn(next(iter(pen_lifts)), range(len(samples)))

    def test_sample_cell_index_keeps_tip_and_ink_in_sync(self):
        # 每个采样点必须带上它归属的 cell 索引，且随采样推进单调不减，
        # 这样“笔尖位置”与“整块揭墨进度”才能严格对应。
        renderer = object.__new__(stream_render.StreamBoardRenderer)
        renderer.cfg = stream_render.Config()

        path = [(0, 0), (0, 1), (0, 2), (0, 3)]
        samples, _pen_lifts, sample_cell_index = renderer._build_stroke_samples(path)

        self.assertEqual(len(samples), len(sample_cell_index))
        # 单调不减
        self.assertEqual(sample_cell_index, sorted(sample_cell_index))
        # 首点归属 cell 0，末点归属最后一个 cell
        self.assertEqual(sample_cell_index[0], 0)
        self.assertEqual(sample_cell_index[-1], len(path) - 1)

    def test_frame_progress_includes_the_last_cursor_position(self):
        renderer = object.__new__(stream_render.StreamBoardRenderer)

        self.assertEqual(renderer._frame_progress_indices(10, 3), [0, 4, 9])

    def test_segment_reveals_only_ink_under_the_current_tip_motion(self):
        renderer = object.__new__(stream_render.StreamBoardRenderer)
        renderer.cfg = stream_render.Config()
        renderer.out_h = 30
        renderer.out_w = 30
        renderer.canvas_bgr = stream_render._hex_to_bgr(renderer.cfg.canvas_hex)
        renderer.drawn = np.full((30, 30, 3), renderer.canvas_bgr, dtype=np.float32)
        renderer.ink_pixels = np.zeros((30, 30), dtype=bool)
        renderer.ink_pixels[15, 4:26] = True
        renderer.ink_paint = np.zeros((30, 30, 3), dtype=np.float32)

        renderer._reveal_ink_segment((4, 15), (10, 15))

        self.assertTrue(np.all(renderer.drawn[15, 4:11] == 0))
        self.assertTrue(np.all(renderer.drawn[15, 20] == renderer.canvas_bgr))

    def test_text_scan_order_progresses_left_to_right(self):
        # 一行横向墨迹，按段扫描应从左到右推进（列单调不减）
        cells = [(0, c) for c in range(10)]
        order = stream_render._text_scan_order(cells, segment_cols=3)

        cols = [col for _, col in order]
        self.assertEqual(sorted(cols), list(range(10)))  # 无丢失
        self.assertEqual(cols, sorted(cols))             # 列单调（左到右）

    def test_text_scan_order_covers_all_cells_grouped_by_column_band(self):
        # 多行文字：段内按行（行内按列）、段间按列从左到右。
        # segment_cols=2 → 三段各 6 格: 列(0,1)/(2,3)/(4,5)
        cells = [(r, c) for r in range(3) for c in range(6)]
        order = stream_render._text_scan_order(cells, segment_cols=2)

        self.assertEqual(sorted(order), sorted(cells))   # 无丢失无重复
        # 第一段在最左两列，最后一段在最右两列
        self.assertTrue(all(c < 2 for _, c in order[:6]))
        self.assertTrue(all(c >= 4 for _, c in order[12:]))
        # 段间整体从左到右：每段的列均值递增
        band_means = [
            sum(c for _, c in order[i:i + 6]) / 6 for i in range(0, 18, 6)
        ]
        self.assertEqual(band_means, sorted(band_means))

    def test_cluster_streams_chain_nearby_components_to_reduce_jumps(self):
        # 两个分离的连通域：主体在左下，一个小轮廓在右上。
        # 串联后第二支应被反向，使其起点（原终点）靠近主体出口。
        active = np.zeros((20, 20), dtype=bool)
        active[10:16, 2:8] = True    # 主体（最大）
        active[1:3, 14:17] = True    # 小轮廓

        streams = stream_render.cluster_ink_streams(active)

        self.assertEqual(len(streams), 2)
        # 第一支是主体（面积大），起点应在主体范围内
        first_start = streams[0][0]
        self.assertTrue(10 <= first_start[0] <= 15 and 2 <= first_start[1] <= 7)
        # 第二支（小轮廓）的起点应比终点更靠近第一支的出口
        first_tail = streams[0][-1]
        second_head = streams[1][0]
        second_tail = streams[1][-1]
        d_head = (second_head[0] - first_tail[0]) ** 2 + (second_head[1] - first_tail[1]) ** 2
        d_tail = (second_tail[0] - first_tail[0]) ** 2 + (second_tail[1] - first_tail[1]) ** 2
        self.assertLessEqual(d_head, d_tail + 1)


if __name__ == "__main__":
    unittest.main()
