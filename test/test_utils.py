from shutil import which
import pytest
import tempfile
from bs4 import BeautifulSoup
from markdown import markdown

import utils
from utils import get_json_from_response, split_markdown_to_chunks


def test_extract_json_from_markdown_block(self):
    """Test extracting JSON from a markdown code block."""
    response = """
    Here's the JSON you requested:
    
    ```json
    {
        "name": "John",
        "age": 30,
        "city": "New York"
    }
    ```
    
    Let me know if you need anything else.
    """

    result = get_json_from_response(response)
    assert isinstance(result, dict)
    assert result["name"] == "John"
    assert result["age"] == 30
    assert result["city"] == "New York"

def test_extract_json_from_text(self):
    """Test extracting JSON directly from text."""
    response = """
    Here's the JSON:
    
    {
        "name": "John",
        "age": 30,
        "city": "New York"
    }
    
    Let me know if you need anything else.
    """

    result = get_json_from_response(response)
    assert isinstance(result, dict)
    assert result["name"] == "John"
    assert result["age"] == 30
    assert result["city"] == "New York"

def test_extract_json_with_repair(self):
    """Test extracting JSON with minor syntax errors that can be repaired."""
    response = """
    Here's the JSON:
    
    {
        'name': 'John',
        'age': 30,
        'city': 'New York'
    }
    
    Let me know if you need anything else.
    """

    result = get_json_from_response(response)
    assert isinstance(result, dict)
    assert result["name"] == "John"
    assert result["age"] == 30
    assert result["city"] == "New York"

def test_extract_nested_json(self):
    """Test extracting nested JSON objects."""
    response = """
    Here's the JSON:
    
    {
        "person": {
            "name": "John",
            "age": 30
        },
        "address": {
            "city": "New York",
            "zip": "10001"
        }
    }
    """

    result = get_json_from_response(response)
    assert isinstance(result, dict)
    assert result["person"]["name"] == "John"
    assert result["address"]["city"] == "New York"

def test_json_not_found(self):
    """Test that an exception is raised when JSON is not found."""
    response = "This is just plain text with no JSON."

    with pytest.raises(Exception) as excinfo:
        get_json_from_response(response)

    assert "JSON not found" in str(excinfo.value)

def test_ppt_to_images_conversion(self):
    """Test converting a PPTX file to images."""
    # Skip if unoconvert is not available
    assert (
        which("unoconvert") is not None and which("unoserver") is not None
    ), "executable `unoconvert` or `unoserver` not available"
    try:
        import subprocess

        subprocess.run(
            ["unoconvert", "--version"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (ImportError, subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("unoconvert not available")

    # Run the conversion
    process = subprocess.Popen(["unoserver"], shell=False)
    utils.UNOSERVER_RUNNING = True
    utils.ppt_to_images("resource/test/test_ppt/test.pptx", tempfile.mkdtemp())
    process.terminate()

def test_markdown_splits():
    markdown_content = open("resource/test/test_pdf/source.md", "r").read()
    chunks = split_markdown_to_chunks(markdown_content)
    assert len(chunks) == 8
    markdown_html = markdown(markdown_content, extensions=["tables"])
    soup = BeautifulSoup(markdown_html, "html.parser")
    num_medias = len(soup.find_all("img")) + len(soup.find_all("table"))
    parsed_medias = 0
    for chunk in chunks:
        markdown_html = markdown(chunk["content"], extensions=["tables"])
        soup = BeautifulSoup(markdown_html, "html.parser")
        parsed_medias += len(soup.find_all("img")) + len(soup.find_all("table"))
    assert parsed_medias == num_medias
