import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from decay_engine import DecayEngine, _metadata_flag


class _BucketManager:
    def __init__(self, buckets):
        self.buckets = buckets
        self.archived_ids = []
        self.updated_ids = []
        self.embedding_engine = None

    async def list_all(self, include_archive=False):
        return list(self.buckets)

    async def update(self, bucket_id, **kwargs):
        self.updated_ids.append((bucket_id, kwargs))
        return True

    async def archive(self, bucket_id):
        self.archived_ids.append(bucket_id)
        return True


def _old_bucket(bucket_id, anchor):
    old = (datetime.now() - timedelta(days=365)).isoformat()
    return {
        "id": bucket_id,
        "content": "same-age low-score memory",
        "metadata": {
            "type": "dynamic",
            "name": bucket_id,
            "importance": 1,
            "activation_count": 1,
            "arousal": 0,
            "created": old,
            "last_active": old,
            "anchor": anchor,
        },
    }


class MetadataFlagTests(unittest.TestCase):
    def test_false_string_is_not_truthy(self):
        self.assertFalse(_metadata_flag("false"))
        self.assertFalse(_metadata_flag("0"))
        self.assertTrue(_metadata_flag("true"))
        self.assertTrue(_metadata_flag(1))


class AnchorDecayExemptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_anchor_is_preserved_while_equivalent_dynamic_is_archived(self):
        anchor = _old_bucket("anchor-memory", True)
        ordinary = _old_bucket("ordinary-memory", "false")
        manager = _BucketManager([anchor, ordinary])
        engine = DecayEngine({"decay": {"threshold": 9999.0}}, manager)

        self.assertLess(engine.calculate_score(anchor["metadata"]), engine.threshold)
        self.assertLess(engine.calculate_score(ordinary["metadata"]), engine.threshold)

        result = await engine.run_decay_cycle()

        self.assertNotIn("anchor-memory", manager.archived_ids)
        self.assertFalse(any(item[0] == "anchor-memory" for item in manager.updated_ids))
        self.assertEqual(manager.archived_ids, ["ordinary-memory"])
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["archived"], 1)


if __name__ == "__main__":
    unittest.main()
