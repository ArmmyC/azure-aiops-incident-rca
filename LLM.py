import os
from dotenv import load_dotenv
from openai import OpenAI


def main():
    load_dotenv()

    client = OpenAI(
        base_url=os.environ["FOUNDRY_ENDPOINT"],
        api_key=os.environ["FOUNDRY_API_KEY"],
    )

    response = client.responses.create(
        model=os.environ["FOUNDRY_MODEL"],
        input="Hello!",
    )

    print("Answer:", response.output_text)
    print("Status:", response.status)

    if response.usage:
        print("Input tokens:", response.usage.input_tokens)
        print("Output tokens:", response.usage.output_tokens)

        if response.usage.output_tokens_details:
            print(
                "Reasoning tokens:",
                response.usage.output_tokens_details.reasoning_tokens,
            )


if __name__ == "__main__":
    main()
