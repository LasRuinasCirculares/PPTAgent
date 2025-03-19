import asyncio
import json
import os
import traceback
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import jsonlines
import torch
from jinja2 import Environment, StrictUndefined
from rich import print

from agent import Agent, AsyncAgent
from apis import API_TYPES, CodeExecutor
from llms import LLM, AsyncLLM
import llms
from presentation import Presentation, SlidePage, StyleArg
from utils import Config, get_slide_content, pexists, pjoin
from layout import Layout
from document import Document, OutlineItem

style = StyleArg.all_true()
style.area = False


@dataclass
class PPTGen(ABC):
    """
    Stage II: Presentation Generation
    An abstract base class for generating PowerPoint presentations.
    It accepts a reference presentation as input, then generates a presentation outline and slides.
    """

    roles: list[str] = field(default_factory=list)

    def __init__(
        self,
        text_embedder: LLM,
        retry_times: int = 3,
        force_pages: bool = False,
        error_exit: bool = False,
        record_cost: bool = True,
        length_factor: float | None = None,
        language_model: LLM = None,
        vision_model: LLM = None,
    ):
        """
        Initialize the PPTGen.

        Args:
            text_model (LLM): The text model for generating content.
            retry_times (int): The number of times to retry failed actions.
            force_pages (bool): Whether to force a specific number of pages.
            error_exit (bool): Whether to exit on error.
            record_cost (bool): Whether to record the cost of generation.
            **kwargs: Additional arguments.
        """
        self.text_embedder = text_embedder
        self.retry_times = retry_times
        self.force_pages = force_pages
        self.error_exit = error_exit
        self.length_factor = length_factor
        self._hire_staffs(record_cost, language_model, vision_model)
        self._initialized = False

    def set_reference(
        self,
        config: Config,
        slide_induction: dict,
        presentation: Presentation,
    ):
        """
        Set the reference presentation and extracted presentation information.

        Args:
            presentation (Presentation): The presentation object.
            slide_induction (dict): The slide induction data.

        Returns:
            PPTGen: The updated PPTGen object.
        """
        self.config = config
        self.presentation = presentation
        self.functional_keys = slide_induction.pop("functional_keys")
        self.layouts = {k: Layout.from_dict(v) for k, v in slide_induction.items()}
        self.layout_names = list(slide_induction.keys())
        self.layout_embeddings = self.text_embedder.get_embedding(self.layout_names)
        self.empty_prs = deepcopy(presentation)
        self._initialized = True
        return self

    def generate_pres(
        self,
        source_doc: Document,
        file_prefix: str = "final",
        num_slides: Optional[int] = None,
        outline: Optional[List[OutlineItem]] = None,
    ):
        """
        Generate a PowerPoint presentation.

        Args:
            source_doc (Document): The source document.
            file_prefix (str): The prefix for the output file.
            num_slides (Optional[int]): The number of slides to generate.
            outline (Optional[List[OutlineItem]]): The outline of the presentation.

        Save:
            final.pptx: The final PowerPoint presentation to the config.RUN_DIR directory.

        Raise:
            ValueError: if failed to generate presentation outline.
        """
        assert self._initialized, "PPTGen not initialized, call `set_reference` first"
        self.source_doc = source_doc
        succ_flag = True
        code_executor = CodeExecutor(self.retry_times)
        if outline is None:
            self.outline = self.generate_outline(num_slides, source_doc)
        else:
            self.outline = outline
        self.simple_outline = "\n".join(
            [
                f"Slide {slide_idx+1}: {item.purpose}"
                for slide_idx, item in enumerate(self.outline)
            ]
        )
        generated_slides = []
        for slide_idx, outline_item in enumerate(self.outline):
            if self.force_pages and slide_idx == num_slides:
                break
            slide = self._generate_slide(slide_idx, outline_item, code_executor)
            if slide is not None:
                generated_slides.append(slide)
                continue
            if self.error_exit:
                succ_flag = False
                break
        self._save_history(code_executor)
        if succ_flag:
            self.empty_prs.slides = generated_slides
            self.empty_prs.save(pjoin(self.config.RUN_DIR, f"{file_prefix}.pptx"))

    def generate_outline(
        self,
        num_slides: int,
        source_doc: Document,
    ):
        """
        Generate an outline for the presentation.

        Args:
            num_slides (int): The number of slides to generate.

        Returns:
            dict: The generated outline.
        """
        assert self._initialized, "PPTGen not initialized, call `set_reference` first"
        outline_file = pjoin(self.config.RUN_DIR, "presentation_outline.json")
        if pexists(outline_file):
            return json.load(open(outline_file, "r"))
        outline = self.staffs["planner"](
            num_slides=num_slides,
            document_overview=source_doc.overview,
        )
        outline = self._valid_outline(outline, source_doc)
        json.dump(
            outline,
            open(outline_file, "w"),
            ensure_ascii=False,
            indent=4,
        )
        return outline

    def _valid_outline(
        self, outline: List[Dict], source_doc: Document, retry: int = 0
    ) -> List[OutlineItem]:
        """
        Validate the generated outline.

        Raises:
            ValueError: If the outline is invalid.
        """
        try:
            outline_items = [OutlineItem(**outline_item) for outline_item in outline]
            for outline_item in outline_items:
                source_doc.index(outline_item.indexs)
            return outline_items
        except ValueError as e:
            print(outline, e)
            if retry < self.retry_times:
                new_outline = self.staffs["planner"].retry(
                    str(e), traceback.format_exc(), retry + 1
                )
                return self._valid_outline(new_outline, source_doc, retry + 1)
            else:
                raise ValueError("Failed to generate outline, tried too many times")

    @abstractmethod
    def _generate_slide(
        self, slide_idx: int, outline_item: OutlineItem, code_executor: CodeExecutor
    ) -> SlidePage | None:
        """
        Generate a slide from the outline item.
        """
        raise NotImplementedError("Subclass must implement this method")

    @abstractmethod
    def synergize(
        self,
        layout: Layout,
        slide_content: str,
        code_executor: CodeExecutor,
    ) -> SlidePage:
        """
        Synergize Agents to generate a slide.

        Returns:
            SlidePage: The generated slide.
        """
        raise NotImplementedError("Subclass must implement this method")

    def _save_history(self, code_executor: CodeExecutor):
        """
        Save the history of code execution, API calls and agent steps.
        """
        os.makedirs(pjoin(self.config.RUN_DIR, "history"), exist_ok=True)
        for role in self.staffs.values():
            role.save_history(pjoin(self.config.RUN_DIR, "history"))
            role.history = []
        if len(code_executor.code_history) == 0:
            return
        with jsonlines.open(
            pjoin(self.config.RUN_DIR, "code_steps.jsonl"), "w"
        ) as writer:
            writer.write_all(code_executor.code_history)
        with jsonlines.open(
            pjoin(self.config.RUN_DIR, "agent_steps.jsonl"), "w"
        ) as writer:
            writer.write_all(code_executor.api_history)

    def _hire_staffs(
        self, record_cost: bool, language_model: LLM = None, vision_model: LLM = None
    ) -> dict[str, Agent]:
        """
        Initialize agent roles and their models
        """
        llm_mapping = {
            "language": language_model or llms.language_model,
            "vision": vision_model or llms.vision_model,
        }
        self.staffs = {
            role: Agent(
                role,
                record_cost=record_cost,
                text_model=self.text_embedder,
                llm_mapping=llm_mapping,
            )
            for role in ["planner"] + self.roles
        }


class PPTAgent(PPTGen):
    """
    A class to generate PowerPoint presentations with a crew of agents.
    """

    roles: list[str] = ["editor", "coder", "layout_selector"]

    def _generate_slide(
        self, slide_idx: int, outline_item: OutlineItem, code_executor: CodeExecutor
    ) -> SlidePage | None:
        """
        Generate a slide from the outline item.
        """
        pregenerated = self.pre_generate(slide_idx, self.source_doc, outline_item)
        layout = self.layout_select(pregenerated)
        return self.synergize(layout, pregenerated, code_executor)

    def pre_generate(self, slide_idx: int, source_doc: Document, outline: OutlineItem):
        """
        Abstractive summarization of the content source, faciliating the selection of the layout and the generation of the slide.
        """
        content_source = outline.retrieve(slide_idx, source_doc)

    def layout_select(self, layout_description: str):
        available_layouts = [layout.overview for layout in self.layouts.values()]
        layout = self.staffs["layout_selector"](
            layout_description=layout_description,
            available_layouts=available_layouts,
        )
        return self.layouts[layout]

    def synergize(
        self,
        layout: Layout,
        slide_content: str,
        code_executor: CodeExecutor,
    ) -> SlidePage:
        """
        Synergize Agents to generate a slide.

        Args:
            layout (Layout): The layout data.
            slide_content (str): The slide content.
            code_executor (CodeExecutor): The code executor object.

        Returns:
            SlidePage: The generated slide.
        """
        editor_output = self.staffs["editor"](
            schema=layout.content_schema,
            outline=self.simple_outline,
            metadata=self.source_doc.metainfo,
            text=slide_content,
        )
        command_list = self._generate_commands(editor_output, layout)
        template_id = layout.get_slide_id(editor_output)
        edit_actions = self.staffs["coder"](
            api_docs=code_executor.get_apis_docs(API_TYPES.Agent.value),
            edit_target=self.presentation.slides[template_id - 1].to_html(),
            command_list="\n".join([str(i) for i in command_list]),
        )
        for error_idx in range(self.retry_times):
            edited_slide: SlidePage = deepcopy(
                self.presentation.slides[template_id - 1]
            )
            feedback = code_executor.execute_actions(edit_actions, edited_slide)
            if feedback is None:
                break
            if error_idx == self.retry_times:
                raise Exception(
                    f"Failed to generate slide, tried too many times at editing\ntraceback: {feedback[1]}"
                )
            edit_actions = self.staffs["coder"].retry(*feedback, error_idx + 1)
        self.empty_prs.build_slide(edited_slide)
        return edited_slide

    def _generate_commands(self, editor_output: dict, layout: Layout, retry: int = 0):
        """
        Generate commands for editing the slide content.

        Args:
            editor_output (dict): The editor output.
            content_schema (dict): The content schema.
            old_data (dict): The old data.
            retry (int): The number of retries.

        Returns:
            list: A list of commands.

        Raises:
            Exception: If command generation fails.
        """
        command_list = []
        command_list = []
        try:
            layout.validate(
                editor_output, self.length_factor, self.source_doc.image_dir
            )
        except Exception as e:
            if retry < self.retry_times:
                new_output = self.staffs["editor"].retry(
                    e,
                    traceback.format_exc(),
                    retry + 1,
                )
                return self._generate_commands(new_output, layout, retry + 1)

        old_data = layout.get_old_data(editor_output)
        for el_name, old_content in old_data.items():
            if not isinstance(old_content, list):
                old_content = [old_content]

            new_content = editor_output[el_name]["data"]
            if not isinstance(new_content, list):
                new_content = [new_content]
            new_content = [i for i in new_content if i]
            quantity_change = len(new_content) - len(old_content)
            command_list.append(
                (
                    el_name,
                    layout.content_schema[el_name]["type"],
                    f"quantity_change: {quantity_change}",
                    old_content,
                    new_content,
                )
            )

        assert len(command_list) > 0, "No commands generated"
        return command_list


class PPTAgentAsync(PPTAgent):
    """
    A class to generate PowerPoint presentations with a crew of agents asynchronously.
    """

    def _hire_staffs(
        self,
        record_cost: bool,
        language_model: AsyncLLM = None,
        vision_model: AsyncLLM = None,
    ) -> dict[str, Agent]:
        """
        Initialize async agent roles and their models
        """
        jinja_env = Environment(undefined=StrictUndefined)
        llm_mapping = {
            "language": language_model or llms.language_model,
            "vision": vision_model or llms.vision_model,
        }
        self.staffs = {
            role: AsyncAgent(
                role,
                env=jinja_env,
                record_cost=record_cost,
                text_model=self.text_embedder,
                llm_mapping=llm_mapping,
            )
            for role in ["planner"] + self.roles
        }

    async def generate_pres(
        self,
        source_doc: Document,
        file_prefix: str = "final",
        num_slides: Optional[int] = None,
        outline: Optional[dict[str, dict]] = None,
    ):
        """
        Generate a PowerPoint presentation asynchronously.

        Args:
            images (dict[str, str]): A dictionary of image paths and captions.
            source_doc (Document): The source document.
            file_prefix (str): The prefix for the output file.
            num_slides (Optional[int]): The number of slides to generate.
            outline (Optional[dict[str, dict]]): The presentation outline.

        Save:
            final.pptx: The final PowerPoint presentation to the config.RUN_DIR directory.
        """
        assert self._initialized, "PPTGen not initialized, call `set_reference` first"
        self.source_doc = source_doc
        succ_flag = True
        code_executor = CodeExecutor(self.retry_times)
        if outline is None:
            self.outline = await self.generate_outline(num_slides, source_doc)
        else:
            self.outline = outline
        self.simple_outline = "\n".join(
            [
                f"Slide {slide_idx+1}: {slide_title}"
                for slide_idx, slide_title in enumerate(self.outline)
            ]
        )

        # Create tasks for all slides
        slide_tasks = []
        for slide_data in enumerate(self.outline.items()):
            if self.force_pages and slide_data[0] == num_slides:
                break
            slide_tasks.append(self._generate_slide_async(slide_data, code_executor))

        # Execute all slide generation tasks concurrently
        generated_slides = []
        results = await asyncio.gather(*slide_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                print(f"Slide generation failed: {result}")
                if self.error_exit:
                    succ_flag = False
                    break
            elif result is not None:
                generated_slides.append(result)

        self._save_history(code_executor)
        if succ_flag:
            self.empty_prs.slides = generated_slides
            self.empty_prs.save(pjoin(self.config.RUN_DIR, f"{file_prefix}.pptx"))

    async def generate_outline(
        self,
        num_slides: int,
        source_doc: Document,
    ):
        """
        Generate an outline for the presentation asynchronously.

        Args:
            num_slides (int): The number of slides to generate.
            source_doc (Document): The source document.

        Returns:
            dict: The generated outline.
        """
        assert self._initialized, "PPTGen not initialized, call `set_reference` first"
        outline_file = pjoin(self.config.RUN_DIR, "presentation_outline.json")
        if pexists(outline_file):
            return json.load(open(outline_file, "r"))

        outline = await self.staffs["planner"](
            num_slides=num_slides,
            document_overview=source_doc.overview,
        )
        outline = await self._valid_outline_async(outline)
        json.dump(
            outline,
            open(outline_file, "w"),
            ensure_ascii=False,
            indent=4,
        )
        return outline

    async def _valid_outline_async(self, outline: dict, retry: int = 0) -> dict:
        """
        Validate the generated outline asynchronously.

        Raises:
            ValueError: If the outline is invalid.
        """
        try:
            for slide in outline.values():
                layout_sim = torch.cosine_similarity(
                    await self.text_embedder.get_embedding(slide["layout"]),
                    self.layout_embeddings,
                )
                # todo 使用edit distance later
                if layout_sim.max() < 0.7:
                    raise ValueError(
                        f"Layout `{slide['layout']}` not found, must be one of {self.layout_names}"
                    )
                slide["layout"] = self.layout_names[layout_sim.argmax().item()]
            if any(
                not {"layout", "subsections", "description"}.issubset(set(slide.keys()))
                for slide in outline.values()
            ):
                raise ValueError(
                    "Invalid outline structure, must be a dict with layout, subsections, description"
                )
        except ValueError as e:
            print(outline, e)
            if retry < self.retry_times:
                new_outline = await self.staffs["planner"].retry(
                    str(e), traceback.format_exc(), retry + 1
                )
                return await self._valid_outline_async(new_outline, retry + 1)
            else:
                raise ValueError("Failed to generate outline, tried too many times")
        return outline

    async def _generate_slide_async(
        self, slide_data, code_executor: CodeExecutor
    ) -> SlidePage | None:
        """
        Generate a slide from the slide data asynchronously.
        """
        slide_idx, (slide_title, slide) = slide_data
        slide_content = f"Slide-{slide_idx+1} " + get_slide_content(
            self.source_doc, slide_title, slide
        )
        try:
            return await self.synergize(
                self.layouts[slide["layout"]],
                slide_content,
                code_executor,
            )
        except Exception as e:
            print(f"generate slide {slide_idx} failed: {e}")
            print(traceback.format_exc())
            print(self.config.RUN_DIR)
            return None

    async def synergize(
        self,
        layout: Layout,
        slide_content: str,
        code_executor: CodeExecutor,
    ) -> SlidePage:
        """
        Synergize Agents to generate a slide asynchronously.

        Args:
            layout (Layout): The layout data.
            slide_content (str): The slide content.
            code_executor (CodeExecutor): The code executor object.

        Returns:
            SlidePage: The generated slide.
        """
        editor_output = await self.staffs["editor"](
            schema=layout.content_schema,
            outline=self.simple_outline,
            metadata=self.source_doc.metainfo,
            text=slide_content,
        )
        command_list = await self._generate_commands_async(editor_output, layout)
        template_id = layout.get_slide_id(editor_output)
        edit_actions = await self.staffs["coder"](
            api_docs=code_executor.get_apis_docs(API_TYPES.Agent.value),
            edit_target=self.presentation.slides[template_id - 1].to_html(),
            command_list="\n".join([str(i) for i in command_list]),
        )
        for error_idx in range(self.retry_times):
            edited_slide: SlidePage = deepcopy(
                self.presentation.slides[template_id - 1]
            )
            feedback = code_executor.execute_actions(edit_actions, edited_slide)
            if feedback is None:
                break
            if error_idx == self.retry_times:
                raise Exception(
                    f"Failed to generate slide, tried too many times at editing\ntraceback: {feedback[1]}"
                )
            edit_actions = await self.staffs["coder"].retry(*feedback, error_idx + 1)
        self.empty_prs.build_slide(edited_slide)
        return edited_slide

    async def _generate_commands_async(
        self, editor_output: dict, layout: Layout, retry: int = 0
    ):
        """
        Generate commands for editing the slide content asynchronously.

        Args:
            editor_output (dict): The editor output.
            layout (Layout): The layout.
            retry (int): The number of retries.

        Returns:
            list: A list of commands.

        Raises:
            Exception: If command generation fails.
        """
        command_list = []
        try:
            layout.validate(
                editor_output, self.length_factor, self.source_doc.image_dir
            )
        except Exception as e:
            if retry < self.retry_times:
                new_output = await self.staffs["editor"].retry(
                    e,
                    traceback.format_exc(),
                    retry + 1,
                )
                return await self._generate_commands_async(
                    new_output, layout, retry + 1
                )

        old_data = layout.get_old_data(editor_output)
        for el_name, old_content in old_data.items():
            if not isinstance(old_content, list):
                old_content = [old_content]

            new_content = editor_output[el_name]["data"]
            if not isinstance(new_content, list):
                new_content = [new_content]
            new_content = [i for i in new_content if i]
            quantity_change = len(new_content) - len(old_content)
            command_list.append(
                (
                    el_name,
                    layout.content_schema[el_name]["type"],
                    f"quantity_change: {quantity_change}",
                    old_content,
                    new_content,
                )
            )

        assert len(command_list) > 0, "No commands generated"
        return command_list
