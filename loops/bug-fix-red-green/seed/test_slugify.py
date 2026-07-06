# seed/test_slugify.py — DO NOT EDIT (gate anchor)
# Python 3.11+
from slugify import slugify


class TestSlugify:
    def test_basic(self):
        assert slugify("Hello World") == "hello-world"

    def test_special_chars(self):
        assert slugify("Hello, World!") == "hello-world"

    def test_multiple_spaces(self):
        # This test FAILS against the buggy version (produces "hello--world").
        assert slugify("Hello  World") == "hello-world"

    def test_leading_trailing_hyphens(self):
        assert slugify(" hello ") == "hello"

    def test_numbers(self):
        assert slugify("article 42") == "article-42"

    def test_already_slug(self):
        assert slugify("already-a-slug") == "already-a-slug"
