import unittest

from cotacoes_ceasa.parsers.ceasa_df import CeasaDfParser


class CeasaDfParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = CeasaDfParser()

    def test_preserves_apple_variety_in_continuation_lines(self) -> None:
        quotes = self.parser.parse_category(
            self._apple_text(
                fuji_commercial="60,00 80,00 90,00",
                gala_commercial="60,00 80,00 90,00",
            ),
            "sima",
            "https://example.com/sima.pdf",
        )

        classifications = [quote.classificacao for quote in quotes]

        self.assertEqual(
            [
                "FUJI CAT-1 TP. 90 A 135",
                "FUJI CAT-1 TP.150 A 175",
                "FUJI COMERCIAL - SOLTA",
                "GALA CAT-1 TP. 90 A 135",
                "GALA CAT-1 TP.150 A 175",
                "GALA COMERCIAL - SOLTA",
            ],
            classifications,
        )

    def test_fuji_and_gala_continuations_remain_distinct_with_equal_prices(self) -> None:
        quotes = self.parser.parse_category(
            self._apple_text(
                fuji_commercial="60,00 80,00 90,00",
                gala_commercial="60,00 80,00 90,00",
            ),
            "sima",
            "https://example.com/sima.pdf",
        )

        quote_keys = {
            (
                quote.classificacao,
                quote.preco_minimo,
                quote.preco_comum,
                quote.preco_maximo,
            )
            for quote in quotes
        }

        classifications = {key[0] for key in quote_keys}
        self.assertEqual(6, len(quotes))
        self.assertEqual(6, len(quote_keys))
        self.assertIn("FUJI CAT-1 TP.150 A 175", classifications)
        self.assertIn("GALA CAT-1 TP.150 A 175", classifications)
        self.assertIn("FUJI COMERCIAL - SOLTA", classifications)
        self.assertIn("GALA COMERCIAL - SOLTA", classifications)

    @staticmethod
    def _apple_text(fuji_commercial: str, gala_commercial: str) -> str:
        return "\n".join(
            (
                "DATA: 06.07.2026",
                "FRUTAS",
                "MAÇÃ - (CX. 18 KG) - PROC. SP/SC/RS/PR.",
                "FUJI CAT-1 TP. 90 A 135 EST 130,00 155,00 175,00",
                "CAT-1 TP.150 A 175 EST 100,00 135,00 148,00",
                f"COMERCIAL - SOLTA EST {fuji_commercial}",
                "GALA CAT-1 TP. 90 A 135 EST 135,00 155,00 175,00",
                "CAT-1 TP.150 A 175 EST 100,00 135,00 148,00",
                f"COMERCIAL - SOLTA EST {gala_commercial}",
            )
        )


if __name__ == "__main__":
    unittest.main()
