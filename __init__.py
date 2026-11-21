"""ComfyUI-LAu-Motion-Transfer: helper nodes and the LAu Motion Transfer workflow for MiniMax H3."""
from typing_extensions import override

from comfy_api.latest import ComfyExtension, io

from .lau_motion.nodes import NODES

WEB_DIRECTORY = "./web"


class LAuMotionExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return NODES


async def comfy_entrypoint() -> LAuMotionExtension:
    return LAuMotionExtension()
