from dataclasses import dataclass, asdict
import traceback
import PIL
from datetime import datetime
from typing import Any, List, Optional, Dict
from uuid import uuid4
import asyncio

from mistune import html as markdown
from bs4 import BeautifulSoup
from jinja2 import Environment, StrictUndefined

from pptagent.llms import LLM, AsyncLLM
from pptagent.agent import Agent, AsyncAgent
from pptagent.utils import (
    markdown_table_to_image,
    package_join,
    pjoin,
    pexists,
    split_markdown_to_chunks,
    pbasename,
    edit_distance,
    get_logger,
)

logger = get_logger(__name__)
env = Environment(undefined=StrictUndefined)
TABLE_CAPTION_PROMPT = env.from_string(
    open(package_join("prompts", "markdown_table_caption.txt")).read()
)
IMAGE_CAPTION_PROMPT = env.from_string(
    open(package_join("prompts", "markdown_image_caption.txt")).read()
)
MERGE_METADATA_PROMPT = env.from_string(
    open(package_join("prompts", "merge_metadata.txt")).read()
)


@dataclass
class Media:
    markdown_content: str
    markdown_caption: str
    path: Optional[str] = None
    caption: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        assert (
            "markdown_content" in data and "markdown_caption" in data
        ), f"'markdown_content' and 'markdown_caption' keys are required in data dictionary but were not found. Input keys: {list(data.keys())}"
        if data.get("path", None) is None:
            assert "---" in data["markdown_content"], "Only table elements have no path"
        return cls(
            markdown_content=data["markdown_content"],
            markdown_caption=data["markdown_caption"],
            path=data.get("path", None),
            caption=data.get("caption", None),
        )

    @property
    def size(self):
        assert self.path is not None, "Path is required to get size"
        return PIL.Image.open(self.path).size


class Table(Media):
    def to_image(self, image_dir: str):
        if self.path is None:
            self.path = pjoin(image_dir, f"table_{str(uuid4())[:4]}.png")
        markdown_table_to_image(self.markdown_content, self.path)
        return self.path


@dataclass
class SubSection:
    title: str
    content: str
    medias: Optional[List[Media]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        assert (
            "title" in data and "content" in data
        ), f"'title' and 'content' keys are required in data dictionary but were not found. Input keys: {list(data.keys())}"
        medias_chunks = data.get("medias", None)
        medias = []
        if medias_chunks is not None:
            for chunk in medias_chunks:
                if chunk.get("path", None) is None:
                    medias.append(Table.from_dict(chunk))
                else:
                    medias.append(Media.from_dict(chunk))
        return cls(
            title=data["title"],
            content=data["content"],
            medias=medias,
        )

    def iter_medias(self):
        if self.medias is not None:
            for media in self.medias:
                yield media


@dataclass
class Section:
    title: str
    subsections: List[SubSection]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        assert (
            "title" in data and "subsections" in data
        ), f"'title' and 'subsections' keys are required in data dictionary but were not found. Input keys: {list(data.keys())}"
        return cls(
            title=data["title"],
            subsections=[
                SubSection.from_dict(subsection) for subsection in data["subsections"]
            ],
        )

    def __getitem__(self, key: str):
        for subsection in self.subsections:
            if subsection.title == key:
                return subsection
        sim_subsec = max(self.subsections, key=lambda x: edit_distance(x.title, key))
        if edit_distance(sim_subsec.title, key) > 0.8:
            return sim_subsec
        raise KeyError(
            f"subsection not found: {key}, available subsections of {self.title} are: {[subsection.title for subsection in self.subsections]}"
        )

    def iter_medias(self):
        for subsection in self.subsections:
            for media in subsection.iter_medias():
                yield media

    def validate_medias(self, image_dir: str, require_caption: bool = True):
        for media in self.iter_medias():
            if media.path is None:
                media.to_image(image_dir)
            elif not pexists(media.path):
                basename = pbasename(media.path)
                if pexists(pjoin(image_dir, basename)):
                    media.path = pjoin(image_dir, basename)
                else:
                    raise FileNotFoundError(
                        f"image file not found: {media.path}, leave null for table elements and real path for image elements"
                    )
            assert (
                media.caption is not None or not require_caption
            ), f"caption is required for media: {media.path}"


@dataclass
class Document:
    image_dir: str
    sections: List[Section]
    metadata: Dict[str, str]

    def __post_init__(self):
        self.metadata["presentation_time"] = datetime.now().strftime("%Y-%m-%d")

    def iter_medias(self):
        for section in self.sections:
            for media in section.iter_medias():
                yield media

    @classmethod
    def from_dict(
        cls, data: Dict[str, Any], image_dir: str, require_caption: bool = True
    ):
        assert (
            "sections" in data
        ), f"'sections' key is required in data dictionary but was not found. Input keys: {list(data.keys())}"
        assert (
            "metadata" in data
        ), f"'metadata' key is required in data dictionary but was not found. Input keys: {list(data.keys())}"
        document = cls(
            image_dir=image_dir,
            sections=[Section.from_dict(section) for section in data["sections"]],
            metadata=data["metadata"],
        )
        for section in document.sections:
            section.validate_medias(image_dir, require_caption)
        return document

    @classmethod
    def _parse_chunk(
        cls,
        extractor: Agent,
        metadata: Optional[Dict[str, Any]],
        section: Optional[Dict[str, Any]],
        image_dir: str,
        num_medias: int,
        retry: int = 0,
        error_exit: bool = False,
    ):
        if retry == 0:
            section = extractor(
                markdown_document=section["content"], num_medias=num_medias
            )
            metadata = section.pop("metadata", {})
        try:
            section = Section.from_dict(section)
            section.validate_medias(image_dir, False)
            parsed_medias = len(list(section.iter_medias()))
            assert (
                parsed_medias == num_medias
            ), f"number of media elements does not match, parsed: {parsed_medias}, expected: {num_medias}"
        except Exception as e:
            logger.error(
                "Failed to extract section, tried %d times, error_exit=%s",
                retry,
                error_exit,
                exc_info=e,
            )
            if retry < 3:
                new_section = extractor.retry(str(e), traceback.format_exc(), retry + 1)
                return cls._parse_chunk(
                    extractor, metadata, new_section, image_dir, num_medias, retry + 1
                )
            else:
                if error_exit:
                    raise ValueError("Failed to extract section, tried too many times")
                else:
                    for subsec in section.subsections:
                        subsec.medias = None
        return metadata, section

    @classmethod
    async def _parse_chunk_async(
        cls,
        extractor: AsyncAgent,
        metadata: Optional[Dict[str, Any]],
        section: Optional[Dict[str, Any]],
        image_dir: str,
        num_medias: int,
        retry: int = 0,
        error_exit: bool = False,
    ):
        if retry == 0:
            section = await extractor(
                markdown_document=section["content"], num_medias=num_medias
            )
            metadata = section.pop("metadata", {})
        try:
            section = Section.from_dict(section)
            section.validate_medias(image_dir, False)
            parsed_medias = len(list(section.iter_medias()))
            assert (
                parsed_medias == num_medias
            ), f"number of media elements does not match, parsed: {parsed_medias}, expected: {num_medias}"
        except Exception as e:
            if retry < 3:
                new_section = await extractor.retry(
                    str(e), traceback.format_exc(), retry + 1
                )
                return await cls._parse_chunk_async(
                    extractor, metadata, new_section, image_dir, num_medias, retry + 1
                )
            else:
                logger.error(
                    "Failed to extract section, tried too many times, error_exit=%s",
                    error_exit,
                    exc_info=e,
                )
                if error_exit:
                    raise ValueError("Failed to extract section, tried too many times")
                else:
                    for subsec in section["subsections"]:
                        subsec["medias"] = None
                    section = Section.from_dict(section)
        return metadata, section

    @classmethod
    def from_markdown(
        cls,
        markdown_content: str,
        language_model: LLM,
        vision_model: LLM,
        image_dir: str,
    ):
        doc_extractor = Agent(
            "doc_extractor",
            llm_mapping={"language": language_model, "vision": vision_model},
        )
        metadata = []
        sections = []
        for chunk in split_markdown_to_chunks(markdown_content):
            if chunk["header"] is not None:
                chunk["content"] = chunk["header"] + "\n" + chunk["content"]
            markdown_html = markdown(chunk["content"])
            soup = BeautifulSoup(markdown_html, "html.parser")
            num_medias = len(soup.find_all("img")) + len(soup.find_all("table"))
            _metadata, _section = cls._parse_chunk(
                doc_extractor, None, chunk, image_dir, num_medias
            )
            metadata.append(_metadata)
            sections.append(_section)
        metadata = language_model(
            MERGE_METADATA_PROMPT.render(metadata=metadata), return_json=True
        )
        document = Document(image_dir=image_dir, metadata=metadata, sections=sections)
        for media in document.iter_medias():
            if isinstance(media, Table):
                media.caption = language_model(
                    TABLE_CAPTION_PROMPT.render(
                        markdown_content=media.markdown_content,
                        markdown_caption=media.markdown_caption,
                    )
                )
            else:
                media.caption = vision_model(
                    IMAGE_CAPTION_PROMPT.render(
                        markdown_caption=media.markdown_caption,
                    ),
                    media.path,
                )
        return document

    @classmethod
    async def from_markdown_async(
        cls,
        markdown_content: str,
        language_model: AsyncLLM,
        vision_model: AsyncLLM,
        image_dir: str,
    ):
        doc_extractor = AsyncAgent(
            "doc_extractor",
            llm_mapping={"language": language_model, "vision": vision_model},
        )

        parse_tasks = []
        for chunk in split_markdown_to_chunks(markdown_content):
            if chunk["header"] is not None:
                chunk["content"] = chunk["header"] + "\n" + chunk["content"]
            markdown_html = markdown(chunk["content"])
            soup = BeautifulSoup(markdown_html, "html.parser")
            num_medias = len(soup.find_all("img")) + len(soup.find_all("table"))

            task = cls._parse_chunk_async(
                doc_extractor, None, chunk, image_dir, num_medias
            )
            parse_tasks.append(task)

        results = await asyncio.gather(*parse_tasks)
        metadata = [meta for meta, _ in results]
        sections = [section for _, section in results]
        merged_metadata = await language_model(
            MERGE_METADATA_PROMPT.render(metadata=metadata), return_json=True
        )
        document = Document(
            image_dir=image_dir, metadata=merged_metadata, sections=sections
        )

        caption_tasks = []
        for media in document.iter_medias():
            if isinstance(media, Table):
                task = language_model(
                    TABLE_CAPTION_PROMPT.render(
                        markdown_content=media.markdown_content,
                        markdown_caption=media.markdown_caption,
                    )
                )
            else:
                task = vision_model(
                    IMAGE_CAPTION_PROMPT.render(
                        markdown_caption=media.markdown_caption,
                    ),
                    media.path,
                )
            caption_tasks.append((media, task))

        for media, task in caption_tasks:
            media.caption = await task

        return document

    def __getitem__(self, key: str):
        for section in self.sections:
            if section.title == key:
                return section
        sim_sec = max(self.sections, key=lambda x: edit_distance(x.title, key))
        if edit_distance(sim_sec.title, key) > 0.8:
            return sim_sec
        raise KeyError(
            f"section not found: {key}, available sections: {[section.title for section in self.sections]}"
        )

    def to_dict(self):
        return asdict(self)

    def index(self, subsection_keys: dict[str, list[str]]) -> List[SubSection]:
        subsecs = []
        for sec_key, subsec_keys in subsection_keys.items():
            section = self[sec_key]
            for subsec_key in subsec_keys:
                subsecs.append(section[subsec_key])
        return subsecs

    @property
    def metainfo(self):
        return "\n".join([f"{k}: {v}" for k, v in self.metadata.items()])

    @property
    def overview(self):
        overview = self.to_dict()
        for section in overview["sections"]:
            for subsection in section["subsections"]:
                subsection.pop("content")
        return overview


@dataclass
class OutlineItem:
    purpose: str
    description: str
    indexs: Dict[str, List[str]]

    def retrieve(self, slide_idx: int, document: Document):
        subsections = document.index(self.indexs)
        header = (
            f"Slide-{slide_idx+1}: {self.purpose}\nDescription: {self.description}\n"
        )
        content = ""
        images = []
        for subsection in subsections:
            content += f"Paragraph: {subsection.title}\nContent: {subsection.content}\n"
            if subsection.medias is not None:
                for media in subsection.medias:
                    images.append(
                        f"Image: {media.path}\nSize: {media.size}\nCaption: {media.caption}"
                    )
        return header, content, "\n".join(images)
