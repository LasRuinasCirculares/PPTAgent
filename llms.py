from copy import deepcopy
import os
from time import sleep, time
import requests
import torch
import logging
import json
from transformers import AutoModel, AutoTokenizer
import google.generativeai as genai
from model_utils import load_image
from presentation import Presentation
from utils import print


class SingletonMeta(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            instance = super().__call__(*args, **kwargs)
            cls._instances[cls] = instance
        return cls._instances[cls]


class InternVL(metaclass=SingletonMeta):
    def __init__(self, model_id="OpenGVLab/InternVL2-8B", device_map: dict = None):
        self._initialized = False
        self._model_id = model_id
        self._device_map = device_map if device_map is not None else {"": 0}

    def _initialize(self):
        self.model = AutoModel.from_pretrained(
            pretrained_model_name_or_path=self._model_id,
            torch_dtype=torch.bfloat16,
            low_cpu_mem_usage=True,
            trust_remote_code=True,
            device_map=self._device_map,
        ).eval()
        self.generation_config = dict(
            num_beams=1,
            max_new_tokens=1024,
            do_sample=False,
        )
        self.tokenizer = AutoTokenizer.from_pretrained(
            self._model_id,
            trust_remote_code=True,
        )
        self._initialized = True

    def __call__(self, pixel_values: torch.Tensor, prompt: str):
        if not self._initialized:
            self._initialize()
        return self.model.chat(
            self.tokenizer,
            pixel_values.to(torch.bfloat16).cuda(),
            prompt,
            self.generation_config,
        )


class Gemini:
    def __init__(self, time_limit: int = 30) -> None:
        proxy = "http://124.16.138.148:7890"
        os.environ["https_proxy"] = proxy
        os.environ["http_proxy"] = proxy
        os.environ["HTTP_PROXY"] = proxy
        os.environ["HTTPS_PROXY"] = proxy
        genai.configure(api_key=os.environ["GOOGLE_API_KEY"])
        self.last_call = 0
        self.time_limit = time_limit
        self.model = genai.GenerativeModel("gemini-1.5-pro-latest")
        self.safety_settings = [
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
        self.generation_config = genai.GenerationConfig(
            response_mime_type="application/json",  # response_schema=list[*DATACLASSES]
        )

    def __call__(self, content: str, image_file: str = None) -> str:
        if image_file is not None:
            image_file = genai.upload_file(image_file)
            content = [image_file, content]
        call_time = time()
        if call_time - self.last_call < self.time_limit:
            sleep(self.time_limit - (call_time - self.last_call))
        self.last_call = call_time
        response = self.model.generate_content(
            content,
            safety_settings=self.safety_settings,
            generation_config=self.generation_config,
        )
        return response.text


class QWEN2:
    def __init__(self) -> None:
        self.api = "http://124.16.138.147:7819/v1/chat/completions"
        self.headers = {"Content-Type": "application/json"}
        self.template_data = {
            "model": "Qwen2-72B-Instruct-GPTQ-Int4",
            "temperature": 0.0,
            "max_tokens": 100,
            "stream": False,
        }

    def __call__(self, content: str) -> str:
        data = deepcopy(self.template_data) | {
            "messages": [{"role": "user", "content": content}]
        }
        response = requests.post(self.api, headers=self.headers, data=json.dumps(data))
        assert response.status_code == 200, response.text

    # 给个markdown的example吧


qwen = QWEN2()
gemini = Gemini()
vl_model = InternVL()


def caption_image(image_file: str):
    _, pixel_values = load_image(image_file)
    prompt = open("resource/prompts/caption.txt")
    return vl_model(pixel_values.to(torch.bfloat16).cuda(), prompt)


# ablation study
def label_image_withslide(
    image_file: str,
    slide_html: str,
    outline: str,
    appear_times: int,
    top_ranges_str: str,
    relative_area: float,
    **kwargs,
):
    prompt_head = open("resource/prompts/prompt_image_withslide.txt").read()
    aspect_ratio, pixel_values = load_image(image_file)
    prompt = (
        prompt_head
        + "Input:\n"
        + {
            "image": "<image>",
            "slide_html": slide_html,
            "outline": outline,
            "appear_times": appear_times,
            "slide_range": top_ranges_str,
            "aspect_ratio": aspect_ratio,
            "relative_area": relative_area,
        }
    )
    return json.loads(vl_model(pixel_values.to(torch.bfloat16).cuda(), prompt))


def label_image_withgemini_image(
    image_file: str,
    appear_times: int,
    top_ranges_str: str,
    relative_area: float,
    caption: str,
    **kwargs,
):
    prompt_head = open("resource/prompts/image_cls_withcap.txt").read()
    aspect_ratio, _ = load_image(image_file)
    prompt = (
        prompt_head
        + "Input:\n"
        + str(
            {
                "caption": caption,
                "appear_times": appear_times,
                "slide_range": top_ranges_str,
                "aspect_ratio": aspect_ratio,
                "relative_area": relative_area,
            }
        )
    )
    return json.loads(gemini(prompt, image_file))


def label_image_withqwen(
    image_file: str,
    appear_times: int,
    top_ranges_str: str,
    relative_area: float,
    caption: str,
    **kwargs,
):
    prompt_head = open("resource/prompts/image_cls_withcap.txt").read()
    aspect_ratio, _ = load_image(image_file)
    prompt = (
        prompt_head
        + "Input:\n"
        + str(
            {
                "caption": caption,
                "appear_times": appear_times,
                "slide_range": top_ranges_str,
                "aspect_ratio": aspect_ratio,
                "relative_area": relative_area,
            }
        )
    )
    return json.loads(qwen(prompt))


def label_image_withcap(
    image_file: str,
    appear_times: int,
    top_ranges_str: str,
    relative_area: float,
    caption: str,
    **kwargs,
):
    prompt_head = open("resource/prompts/image_cls_withcap.txt").read()
    aspect_ratio, pixel_values = load_image(image_file)
    prompt = (
        prompt_head
        + "Input:\n"
        + str(
            {
                "image": "<image>",
                "caption": caption,
                "appear_times": appear_times,
                "slide_range": top_ranges_str,
                "aspect_ratio": aspect_ratio,
                "relative_area": relative_area,
            }
        )
    )
    return json.loads(vl_model(pixel_values.to(torch.bfloat16).cuda(), prompt))


def label_image_withcap_outline(
    image_file: str,
    outline: str,
    appear_times: int,
    top_ranges_str: str,
    relative_area: float,
    caption: str,
    **kwargs,
):
    prompt_head = open("resource/prompts/image_cls_withcap.txt").read()
    aspect_ratio, pixel_values = load_image(image_file)
    prompt = (
        prompt_head
        + "Input:\n"
        + str(
            {
                "image": "<image>",
                "caption": caption,
                "outline": outline,
                "appear_times": appear_times,
                "slide_range": top_ranges_str,
                "aspect_ratio": aspect_ratio,
                "relative_area": relative_area,
            }
        )
    )
    return json.loads(vl_model(pixel_values.to(torch.bfloat16).cuda(), prompt))


def label_image_withoutcap(
    image_file: str,
    outline: str,
    appear_times: int,
    top_ranges_str: str,
    relative_area: float,
    **kwargs,
):
    prompt_head = open("resource/prompts/prompt_image.txt").read()
    aspect_ratio, pixel_values = load_image(image_file)
    prompt = (
        prompt_head
        + "Input:\n"
        + str(
            {
                "image": "<image>",
                "outline": outline,
                "appear_times": appear_times,
                "slide_range": top_ranges_str,
                "aspect_ratio": aspect_ratio,
                "relative_area": relative_area,
            }
        )
    )
    return json.loads(vl_model(pixel_values.to(torch.bfloat16).cuda(), prompt))


OUTLINE_PROMPT = """Extract the most important headings from the provided outline and present them in JSON format. Each heading should have a title and a description string. Ensure the language of the headings matches the input language, and each heading is no longer than three sentences. Example format:
{
"2022 Work Report": "Reporting unit xx, Reporter yy, Reporting date zz",
"Report Contents": "..."
}"""


def get_prs_outline(presentation: Presentation):
    prompt = "\n".join(
        [
            "From the following slides html code which is the content of the presentation.",
            str(presentation),
            OUTLINE_PROMPT,
        ]
    )
    return gemini(prompt)


def get_paper_outline(paper_md: str):
    prompt = "\n".join(
        [
            "From the following text which contains a set of headings and some content within each heading",
            paper_md,
            OUTLINE_PROMPT,
        ]
    )
    return gemini(prompt)


if __name__ == "__main__":
    # internvl = InternVL()
    # internvl.label_image("output/images/图片 2.jpg", 2, "1,3", 0.5)
    print(qwen("你是谁"))
    # gemini = Gemini()
    # print(gemini.chat("你是谁"))
