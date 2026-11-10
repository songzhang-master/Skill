import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, MultiLineString, box

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from merit.merit_basins import clip_reaches, export_bbox_shapefile


class BboxShapefileTest(unittest.TestCase):
    def setUp(self):
        self.bbox = box(0, 0, 1, 1)
        corner = LineString([(-1, -1), (0, 0)])
        crossing = MultiLineString([corner, [(-1, .4), (2, .4)], [(-1, .8), (2, .8)]])
        self.frame = gpd.GeoDataFrame(
            {'COMID': ['17', '29'], 'NextDownID': ['29', '0'], 'lengthkm': [300., 100.]},
            geometry=[crossing, corner], crs=4326)

    def test_mixed_intersection_keeps_lines_and_source_attributes(self):
        clipped = clip_reaches(self.frame, self.bbox)
        self.assertEqual(list(clipped.COMID), ['17'])
        self.assertEqual(len(clipped.geometry.iloc[0].geoms), 2)
        self.assertGreater(clipped.clip_km.iloc[0], 0)
        with tempfile.TemporaryDirectory() as tmp:
            path, bundle, _ = export_bbox_shapefile(clipped, self.bbox, Path(tmp))
            with zipfile.ZipFile(bundle) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(set(archive.namelist()), {path.stem + ext for ext in
                                 ('.shp', '.shx', '.dbf', '.prj', '.cpg')})
            restored = gpd.read_file(path)
            self.assertEqual(restored.crs.to_epsg(), 4326)
            self.assertEqual(restored.NextDownID.tolist(), [29])
            self.assertEqual(restored.lengthkm.tolist(), [300.])
            self.assertEqual(export_bbox_shapefile(clipped, self.bbox, Path(tmp))[0], path)

    def test_point_touch_and_empty_selection_stop(self):
        for frame in (self.frame.iloc[1:], self.frame.iloc[:0]):
            with self.assertRaisesRegex(ValueError, '非零长度'):
                clip_reaches(frame, self.bbox)


if __name__ == '__main__':
    unittest.main()
