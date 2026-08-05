# -*- coding: utf-8 -*-
"""Pruebas del almacenamiento independiente de Chat y Pedidos."""

# [MAXTER CHAT STORAGE TESTS - START: IMPORTS]
import os
import tempfile
import unittest
from pathlib import Path
# [MAXTER CHAT STORAGE TESTS - END: IMPORTS]


class ChatStorageTestCase(unittest.TestCase):
    # [MAXTER CHAT STORAGE TESTS - START: PREPARACIÓN]
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_env = {
            "CHAT_STORAGE_ENABLED": os.environ.get("CHAT_STORAGE_ENABLED"),
            "CHAT_DB_PATH": os.environ.get("CHAT_DB_PATH"),
            "DATABASE_URL": os.environ.get("DATABASE_URL"),
        }
        os.environ["CHAT_STORAGE_ENABLED"] = "true"
        os.environ["CHAT_DB_PATH"] = str(Path(self.temp_dir.name) / "maxter_test.sqlite3")
        os.environ.pop("DATABASE_URL", None)

        from backend.chat_storage import ChatStorage
        self.storage = ChatStorage()

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()
    # [MAXTER CHAT STORAGE TESTS - END: PREPARACIÓN]

    # [MAXTER CHAT STORAGE TESTS - START: CASO PRINCIPAL]
    def test_chat_orders_and_request_deduplication(self):
        metadata = {
            "request_id": "chat-request-1",
            "session_id": "session-1",
            "visitor_id": "visitor-1",
            "page_url": "https://master.com.mx/products/prueba",
            "page_title": "Producto de prueba",
            "referrer": "https://master.com.mx/",
            "user_agent": "unittest",
        }

        self.assertTrue(self.storage.record_chat_exchange(
            metadata=metadata,
            user_message="Busco un sensor para tinaco",
            assistant_message="Encontré estas opciones.",
            effective_query="sensor tinaco",
            page=1,
            products=[{"title": "Sensor", "sku": "TEST-1"}],
        ))

        # El mismo request_id no debe crear una segunda fila.
        self.assertTrue(self.storage.record_chat_exchange(
            metadata=metadata,
            user_message="Busco un sensor para tinaco",
            assistant_message="Respuesta duplicada",
            effective_query="sensor tinaco",
            page=1,
            products=[],
        ))

        order_metadata = dict(metadata, request_id="order-request-1")
        self.assertTrue(self.storage.record_order_query(
            metadata=order_metadata,
            order_number="702-1234567-1234567",
            found=True,
            items=[{"SKU de producto": "TEST-1"}],
            answer="Pedido encontrado",
        ))

        chat = self.storage.list_records("chat", limit=100)
        orders = self.storage.list_records("orders", limit=100)
        status = self.storage.status()

        self.assertEqual(chat["total"], 1)
        self.assertEqual(orders["total"], 1)
        self.assertEqual(status["counts"]["chat_exchanges"], 1)
        self.assertEqual(status["counts"]["order_queries"], 1)
        self.assertEqual(status["counts"]["sessions"], 1)
        self.assertEqual(chat["items"][0]["products"][0]["sku"], "TEST-1")
        self.assertTrue(orders["items"][0]["found"])
    # [MAXTER CHAT STORAGE TESTS - END: CASO PRINCIPAL]


if __name__ == "__main__":
    unittest.main()
