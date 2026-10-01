"""跨工厂啤酒批次放行平台。

在基础服务（经营主体、操作者、站点、幂等回执、哈希审计链）之上提供
品牌标准版本、批次谱系、实验室结果、设备校准、偏差与放行决定等能力。
"""

from .service import BatchReleaseService

__all__ = ["BatchReleaseService"]
