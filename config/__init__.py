# config/__init__.py
import os
import pathlib
from datetime import datetime
from typing import Optional

import hydra
# import omegaconf
# from hydra import GlobalHydra
# Hydra 1.0 - 1.1: from hydra import GlobalHydra
# Hydra 1.2+: from hydra.core.global_hydra import GlobalHydra
from hydra.core.global_hydra import GlobalHydra  # 正确的导入路径
import threading
from omegaconf import OmegaConf, DictConfig

class HydraConfigLoader:
    """
    Hydra配置加载器类，处理绝对路径和相对路径配置文件
    """
    _instance = None
    _lock = threading.Lock()
    _hydra_initialized = False
    _initialized_config_dir = None

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def ensure_hydra_initialized(cls, config_dir: Optional[str] = None):
        """
        确保 Hydra 只初始化一次
        
        Args:
            config_dir: 配置目录路径，如果为None则使用当前文件所在目录
        """
        if config_dir is None:
            config_dir = str(pathlib.Path(__file__).parent.absolute())

        with cls._lock:
            # 检查是否需要重新初始化（配置目录不同）
            if (cls._hydra_initialized and 
                cls._initialized_config_dir != config_dir):
                # 需要清理并重新初始化
                if GlobalHydra.instance().is_initialized():
                    GlobalHydra.instance().clear()
                cls._hydra_initialized = False
                cls._initialized_config_dir = None

            # 初始化 Hydra（如果尚未初始化或配置目录已更改）
            if not cls._hydra_initialized:
                # 清理可能存在的已有实例
                if GlobalHydra.instance().is_initialized():
                    GlobalHydra.instance().clear()
                
                # 初始化 Hydra
                hydra.initialize_config_dir(
                    config_dir=config_dir,
                    version_base=None
                )
                cls._hydra_initialized = True
                cls._initialized_config_dir = config_dir

    @classmethod
    def load_config(cls, 
                   config_path: str, 
                   *overrides, 
                   config_dir: Optional[str] = None,
                   create_timestamped_dir: bool = True) -> DictConfig:
        """
        Load configuration with optional timestamped output directory.
        
        Args:
            config_path: Path to config file (can be absolute or relative to config_dir)
            *overrides: Hydra override parameters
            config_dir: Configuration directory. If None, uses current file's parent directory
            create_timestamped_dir: Whether to create a timestamped subdirectory
        
        Returns:
            Resolved configuration object
        """
        if config_dir is None:
            config_dir = str(pathlib.Path(__file__).parent.absolute())
        
        cls.ensure_hydra_initialized(config_dir)
        
        # 处理配置路径
        config_path_obj = pathlib.Path(config_path)
        
        if config_path_obj.is_absolute():
            # 如果是绝对路径，则确定配置名称和配置目录
            config_dir_path = pathlib.Path(config_dir).resolve()
            
            # 尝试找到相对于配置目录的路径
            try:
                # 计算相对于配置目录的路径
                relative_path = config_path_obj.relative_to(config_dir_path)
                config_name = str(relative_path.with_suffix(''))
            except ValueError:
                # 如果配置文件不在配置目录下，需要特殊处理
                # 提取文件名作为配置名
                config_name = config_path_obj.stem
                print(f"Warning: Config file {config_path} is outside config_dir {config_dir}. "
                      f"Using filename only: {config_name}")
        else:
            # 如果是相对路径，移除扩展名得到配置名
            config_name = config_path
            if config_name.startswith("config/"):
                config_name = config_name[7:]
            if config_name.endswith((".yaml", ".yml")):
                config_name = config_name[:-5] if config_name.endswith('.yaml') else config_name[:-4]

        print(f"Final config_name: {config_name}")
        print(f"Config dir: {config_dir}")

        # Check if output_dir is explicitly specified in overrides
        output_dir_override = None
        for override in overrides:
            if override.startswith("output_dir="):
                output_dir_override = override.split("=", 1)[1]
                break

        # Load and resolve configuration
        cfg = hydra.compose(config_name=config_name, overrides=list(overrides))
        cfg = OmegaConf.create(
            OmegaConf.to_container(cfg, resolve=True)
        )

        # Handle output directory creation
        if create_timestamped_dir and output_dir_override is None:
            # Create timestamped output directory
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = pathlib.Path(cfg.output_dir) / f"{config_name}_{timestamp}"
        else:
            # Use the existing output_dir
            output_dir = pathlib.Path(cfg.output_dir)

        # Create directory and update config
        output_dir.mkdir(parents=True, exist_ok=True)
        cfg.output_dir = str(output_dir)

        return cfg

    @classmethod
    def reset(cls):
        """重置初始化状态，用于测试或重新配置"""
        with cls._lock:
            if cls._hydra_initialized and GlobalHydra.instance().is_initialized():
                GlobalHydra.instance().clear()
            cls._hydra_initialized = False
            cls._initialized_config_dir = None


def load_config(config_path: str, 
               *overrides, 
               config_dir: Optional[str] = None,
               create_timestamped_dir: bool = True) -> DictConfig:
    """
    Load configuration with timestamped output directory.
    This is a convenience function that delegates to HydraConfigLoader.
    """
    loader = HydraConfigLoader()
    return loader.load_config(
        config_path, 
        *overrides, 
        config_dir=config_dir,
        create_timestamped_dir=create_timestamped_dir
    )


def extract_config_name_and_dir(config_path: str, default_config_dir: Optional[str] = None):
    """
    从配置路径中提取配置名称和配置目录
    这是一个辅助函数，用于调试配置路径问题
    """
    if default_config_dir is None:
        default_config_dir = str(pathlib.Path(__file__).parent.absolute())
    
    config_path_obj = pathlib.Path(config_path)
    
    if config_path_obj.is_absolute():
        # 找到配置文件所在的目录
        config_file_dir = config_path_obj.parent
        config_name = config_path_obj.stem
        
        # 如果配置文件就在默认配置目录下，使用默认目录
        default_config_dir_path = pathlib.Path(default_config_dir)
        if config_file_dir == default_config_dir_path:
            final_config_dir = default_config_dir
        else:
            # 否则使用配置文件所在目录
            final_config_dir = str(config_file_dir)
    else:
        # 相对路径：计算相对于默认配置目录的路径
        config_name = config_path
        if config_name.startswith("config/"):
            config_name = config_name[7:]
        if config_name.endswith((".yaml", ".yml")):
            config_name = config_name[:-5] if config_name.endswith('.yaml') else config_name[:-4]
        final_config_dir = default_config_dir
    
    return config_name, final_config_dir
