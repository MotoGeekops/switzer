"""Discord card formatting and API size constraints."""

from types import SimpleNamespace
import unittest

from switzer_ui import (
    COLOR_AI, COLOR_REMINDER, COLOR_TASK, answer_embeds, clean_markdown,
    message_text, page_embeds, plain_text, split_pages, status_embed, task_embeds,
)


class CardTests(unittest.TestCase):
    def test_markdown_cleanup_preserves_code_and_csharp_heading(self):
        source = '# C#\n\n---\n**\n\n```python\n# comment\n---\n**\n```'
        result = clean_markdown(source)
        self.assertTrue(result.startswith('**C#**'))
        self.assertIn('```python\n# comment\n---\n**\n```', result)
        self.assertNotIn('\n---\n', result.split('```')[0])

    def test_normal_paragraphs_retain_every_word_in_order(self):
        source = '\n\n'.join(' '.join(f'word{n}' for n in range(i, i+150)) for i in range(0, 1500, 150))
        pages = split_pages(source)
        self.assertGreater(len(pages), 1)
        self.assertEqual(' '.join(source.split()), ' '.join(' '.join(pages).split()))
        self.assertTrue(all(len(page) <= 4000 for page in pages))

    def test_long_code_blocks_keep_language_and_balanced_fences(self):
        source = '```python\n' + ''.join(f'print({n})\n' for n in range(1500)) + '```'
        pages = split_pages(source)
        for page in pages:
            self.assertLessEqual(len(page), 4000)
            self.assertTrue(page.startswith('```python\n'))
            self.assertTrue(page.endswith('```'))
            self.assertEqual(page.count('```'), 2)
        rendered = '\n'.join(page[len('```python\n'):-3].strip() for page in pages)
        self.assertEqual(rendered.splitlines(), source[len('```python\n'):-3].strip().splitlines())

    def test_unclosed_code_fences_are_repaired(self):
        for length in (50, 3990, 4000, 8100):
            with self.subTest(length=length):
                pages = split_pages('```\n' + 'x' * length)
                self.assertTrue(all(len(page) <= 4000 and page.endswith('```') for page in pages))

    def test_small_invalid_page_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            split_pages('text', limit=0)

    def test_page_embed_limits_and_page_footers(self):
        cards = page_embeds('hello ' * 2500, title='T'*1000, footer='F'*5000, color=COLOR_AI)
        for card in cards:
            self.assertLessEqual(len(card.description), 4096)
            self.assertLessEqual(len(card), 6000)
            self.assertLessEqual(len(card.footer.text), 2048)
        self.assertEqual(len(cards[0].title), 256)
        self.assertIsNone(cards[1].title)
        normal = answer_embeds('text ' * 2000)
        self.assertIn(f'Page 1/{len(normal)}', normal[0].footer.text)

    def test_status_limits_include_total_field_budget(self):
        card = status_embed('T'*500, 'D'*5000, fields=[('N'*500, 'V'*2000)]*40, footer='F'*4000)
        self.assertLessEqual(len(card), 6000)
        self.assertLessEqual(len(card.fields), 25)
        for field in card.fields:
            self.assertLessEqual(len(field.name), 256)
            self.assertLessEqual(len(field.value), 1024)

    def test_reminder_card_keeps_body_without_duplicate_greeting(self):
        card = task_embeds({'id':'abc123'}, 'Hey <@42>! Time to **feed your dog**!', 'reminder')[0]
        self.assertEqual(card.colour.value, COLOR_REMINDER)
        self.assertEqual(card.description, 'Time to **feed your dog**!')
        self.assertNotIn('<@42>', plain_text(embed=card))
        task = task_embeds({'id':'abc123', 'prompt':'Review essay'}, 'Actual review', 'task')[0]
        self.assertEqual(task.colour.value, COLOR_TASK)

    def test_context_reads_card_body_fields_and_plain_messages(self):
        card = status_embed('Original essay', 'Full original text', fields=[('When','5 minutes')])
        result = message_text(SimpleNamespace(content='User note', embeds=[card]))
        for expected in ('User note', 'Original essay', 'Full original text', '5 minutes'):
            self.assertIn(expected, result)

    def test_empty_answer_still_has_valid_embed_description(self):
        cards = answer_embeds('')
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0].description)


if __name__ == '__main__':
    unittest.main()
