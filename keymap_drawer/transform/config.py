"""Configuration models for keymap transforms."""

from pydantic import BaseModel, Field


class TransformConfig(BaseModel):
    """Configuration for transform behavior."""

    hidden_corner_legends: list[str] = Field(
        default_factory=list,
        description="Values to hide in corner positions (e.g., modifier symbols)",
    )
    hidden_held_legends: list[str] = Field(
        default_factory=list,
        description='Hold legend values to hide (e.g., "sticky")',
    )
    hidden_shifted_legends: list[str] = Field(
        default_factory=list,
        description="Shifted legend values to hide",
    )
