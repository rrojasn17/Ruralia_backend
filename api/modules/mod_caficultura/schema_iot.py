from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator


NodeType = Literal["ttn", "microcontrolador"]
IntegrationProvider = Literal["ttn"]
WidgetKind = Literal["line", "area", "bar", "stat", "gauge", "clock"]


def normalize_node_did(value: str, node_type: str | None = None) -> str:
    raw = str(value or "").strip()
    if node_type == "ttn":
        raw = re.sub(r"[-:\s]", "", raw).upper()
        if not re.fullmatch(r"[0-9A-F]{16}", raw):
            raise ValueError(
                "El devEUI TTN debe contener exactamente 16 caracteres hexadecimales"
            )
        return raw
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_-]{3,99}", raw):
        raise ValueError(
            "El DID debe tener de 4 a 100 caracteres alfanuméricos, guion, dos puntos o guion bajo"
        )
    return raw.upper()


def normalize_ttn_application_id(value: str) -> str:
    raw = str(value or "").strip().lower()
    # The Things Stack: 3-36 caracteres, minúsculas/números y guiones internos.
    if not re.fullmatch(r"[a-z0-9](?:-?[a-z0-9]){2,35}", raw):
        raise ValueError(
            "Application ID TTN inválido. Use 3 a 36 caracteres en minúscula, números y guiones."
        )
    return raw


class IoTIntegrationCreate(BaseModel):
    nombre: str = Field(min_length=2, max_length=180)
    provider: IntegrationProvider = "ttn"
    application_id: str = Field(min_length=3, max_length=36)
    activo: bool = True

    @field_validator("nombre")
    @classmethod
    def clean_name(cls, value: str) -> str:
        clean = value.strip()
        if len(clean) < 2:
            raise ValueError("El nombre debe tener al menos 2 caracteres")
        return clean

    @field_validator("application_id")
    @classmethod
    def clean_application_id(cls, value: str) -> str:
        return normalize_ttn_application_id(value)


class IoTIntegrationUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=2, max_length=180)
    application_id: str | None = Field(default=None, min_length=3, max_length=36)
    activo: bool | None = None

    @field_validator("nombre")
    @classmethod
    def clean_optional_name(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("application_id")
    @classmethod
    def clean_optional_application_id(cls, value: str | None) -> str | None:
        return normalize_ttn_application_id(value) if value is not None else None


class IoTIntegrationOut(BaseModel):
    id: int
    nombre: str
    provider: str
    integration_key: str
    application_id: str | None = None
    activo: bool
    webhook_path: str
    header_name: str = "X-TTN-Webhook-Secret"
    node_count: int = 0
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class IoTIntegrationCreatedOut(IoTIntegrationOut):
    webhook_secret: str


class IoTIntegrationSecretOut(BaseModel):
    integration_id: int
    webhook_secret: str


class IoTVariableConfig(BaseModel):
    # ``key_ttn`` mantiene compatibilidad con el patrón usado en BOTAAP;
    # RuralIA serializa siempre como ``source_key`` para poder reutilizar el
    # mismo modelo con la API propia de microcontroladores.
    source_key: str = Field(
        min_length=1,
        max_length=120,
        validation_alias=AliasChoices("source_key", "key_ttn"),
    )
    label: str = Field(min_length=1, max_length=180)
    unit: str = Field(default="", max_length=30)
    color: str = Field(default="#176B75", max_length=20)
    precision: int = Field(default=2, ge=0, le=6)
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)

    @field_validator("source_key", "label")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("La clave de origen y la etiqueta no pueden estar vacías")
        return clean

    @field_validator("unit", "color")
    @classmethod
    def strip_optional_text(cls, value: str) -> str:
        return value.strip()


class IoTNodeCreate(BaseModel):
    finca_id: int = Field(gt=0)
    integration_id: int | None = Field(default=None, gt=0)
    nombre: str = Field(min_length=2, max_length=180)
    did: str = Field(min_length=4, max_length=100)
    tipo: NodeType = "ttn"
    activo: bool = True
    variables_config: dict[str, IoTVariableConfig] = Field(default_factory=dict)

    @field_validator("nombre")
    @classmethod
    def clean_name(cls, value: str) -> str:
        clean = value.strip()
        if len(clean) < 2:
            raise ValueError("El nombre debe tener al menos 2 caracteres")
        return clean

    @model_validator(mode="after")
    def normalize_identifier(self):
        self.did = normalize_node_did(self.did, self.tipo)
        if self.tipo == "ttn" and not self.integration_id:
            raise ValueError("Seleccione la integración TTN a la que pertenece el sensor")
        if self.tipo != "ttn" and self.integration_id is not None:
            raise ValueError("La integración TTN solo aplica a sensores tipo TTN")
        return self


class IoTNodeUpdate(BaseModel):
    finca_id: int | None = Field(default=None, gt=0)
    integration_id: int | None = Field(default=None, gt=0)
    nombre: str | None = Field(default=None, min_length=2, max_length=180)
    did: str | None = Field(default=None, min_length=4, max_length=100)
    tipo: NodeType | None = None
    activo: bool | None = None
    variables_config: dict[str, IoTVariableConfig] | None = None

    @field_validator("nombre")
    @classmethod
    def clean_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = value.strip()
        if len(clean) < 2:
            raise ValueError("El nombre debe tener al menos 2 caracteres")
        return clean


class IoTNodeOut(BaseModel):
    id: int
    finca_id: int
    finca_nombre: str
    cliente_id: int
    cliente_nombre: str
    integration_id: int | None = None
    integration_name: str | None = None
    ttn_application_id: str | None = None
    nombre: str
    did: str
    tipo: str
    activo: bool
    variables_config: dict[str, dict[str, Any]] = Field(default_factory=dict)
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class IoTNodeCreatedOut(IoTNodeOut):
    ingest_token: str | None = None


class IoTCredentialOut(BaseModel):
    node_id: int
    did: str
    ingest_token: str


class IoTMicrocontrollerIn(BaseModel):
    timestamp: datetime | None = None
    event_id: str | None = Field(default=None, max_length=180)
    data: dict[str, Any]


class IoTReadingOut(BaseModel):
    id: int
    node_id: int
    recorded_at: datetime
    received_at: datetime
    values: dict[str, float | int]


class IoTTelemetryOut(BaseModel):
    finca: dict[str, Any]
    from_ts: datetime
    to_ts: datetime
    nodes: list[IoTNodeOut]
    readings: list[IoTReadingOut]
    truncated: bool = False
    calculated: dict[str, Any] = Field(default_factory=dict)


class IoTFormulaInput(BaseModel):
    node_id: int = Field(gt=0)
    variable_id: str = Field(min_length=1, max_length=80)


class IoTWidgetIn(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    node_id: int = Field(gt=0)
    variable_id: str = Field(min_length=1, max_length=80)
    kind: WidgetKind = "line"
    hidden: bool = False
    title: str | None = Field(default=None, max_length=180)
    color: str | None = Field(
        default=None, max_length=20, pattern=r"^(#[0-9A-Fa-f]{3,8}|[a-zA-Z]{3,20})$"
    )
    decimals: int = Field(default=2, ge=0, le=6)
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)
    order: int = Field(default=1, ge=1, le=500)
    formula: str | None = Field(default=None, min_length=1, max_length=500)
    inputs: dict[str, IoTFormulaInput] = Field(default_factory=dict, max_length=16)
    unit: str | None = Field(default=None, max_length=30)
    max_gap_minutes: int = Field(default=15, ge=0, le=1440)

    @model_validator(mode="after")
    def validate_formula(self):
        from modules.mod_caficultura.iot_formulas import FUNCTIONS, parse_formula

        if self.formula is not None:
            if not self.inputs:
                raise ValueError("Seleccione las variables de la fórmula")
            if any(not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,29}", alias) or alias in FUNCTIONS for alias in self.inputs):
                raise ValueError("Alias de variable inválido o reservado")
            parse_formula(self.formula, set(self.inputs))
            anchor = next(iter(self.inputs.values()))
            if (self.node_id, self.variable_id) != (anchor.node_id, anchor.variable_id):
                raise ValueError("El sensor y variable base deben coincidir con la primera entrada")
        elif self.inputs:
            raise ValueError("Indique una fórmula para las variables seleccionadas")
        if self.minimum is not None and self.maximum is not None and self.minimum >= self.maximum:
            raise ValueError("El mínimo debe ser menor que el máximo")
        return self


class IoTDashboardPresetIn(BaseModel):
    widgets: list[IoTWidgetIn] = Field(default_factory=list, max_length=100)


class IoTDashboardPresetOut(BaseModel):
    id: int | None = None
    finca_id: int
    widgets: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: datetime | None = None


class IoTTtnConfigOut(BaseModel):
    integration_webhook_template: str = "/iot/ingest/ttn/{integration_key}"
    header_name: str = "X-TTN-Webhook-Secret"
    integration_count: int = 0
    legacy_webhook_path: str = "/iot/ingest/ttn"
    legacy_webhook_enabled: bool = False
    device_identifier: str = "dev_eui"
    event_type: str = "uplink_message"
    payload_field: str = "uplink_message.decoded_payload"


class IoTNodeStatusOut(BaseModel):
    node: IoTNodeOut
    has_data: bool
    reading_count: int = 0
    latest_reading_at: datetime | None = None
    latest_received_at: datetime | None = None
    latest_source: str | None = None
    latest_values: dict[str, float | int] = Field(default_factory=dict)
    latest_source_keys: list[str] = Field(default_factory=list)
