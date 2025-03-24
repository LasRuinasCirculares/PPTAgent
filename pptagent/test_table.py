from presentation import Presentation
from apis import replace_image_with_table, replace_image
from shapes import Picture
from utils import Config
from pptx import Presentation as PPTXPre
from copy import deepcopy

config = Config('/root/pptagent/pptagent-v2/template')
presentation = Presentation.from_file("/root/pptagent/pptagent-v3/template/source.pptx", config)
slide_page = presentation.slides[0]
picture = next((s for s in slide_page.shapes if isinstance(s, Picture)), None)
if picture is None:
    print("No picture found in the slide.")
else:
    shape_idx = picture.shape_idx
    table_data = [["种类", "价格"], ["苹果", "5"]]
    replace_image_with_table(slide_page, shape_idx, table_data)
    presentation.save('modified.pptx')
    print("Image replaced with table and saved to modified.pptx")