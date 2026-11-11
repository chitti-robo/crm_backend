from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    DATABASE_URL: str = "sqlite:///./dev.db"
    PUBLIC_API_KEY: str = "change-me"
    META_APP_SECRET: str = ""
    META_VERIFY_TOKEN: str = "change-me"
    META_PAGE_TOKEN: str = ""
    WHATSAPP_TOKEN: str = ""
    WHATSAPP_PHONE_ID: str = ""
    WHATSAPP_WABA_ID: str = ""
    WHATSAPP_TEMPLATE: str = "student_followup"
    WHATSAPP_TEMPLATE_LANG: str = "en"
    WHATSAPP_VERIFY_TOKEN: str = "change-me"
    GOOGLE_ADS_WEBHOOK_KEY: str = "change-me"
    LINKEDIN_ACCESS_TOKEN: str = ""
    LINKEDIN_FORM_COURSE_MAP: dict[str, str] = {}
    GOOGLE_SERVICE_ACCOUNT_JSON: str = ""
    GSC_SITE_URL: str = ""
    SHEETS_WEBHOOK_URL: str = ""
    GA4_PROPERTY_ID: str = ""
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""


settings = Settings()
