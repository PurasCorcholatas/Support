
def calculate_security_score(answers, real_data, question_bank):

    score = 0
    total_weight = 0
    analysis = {}

    for key, user_answer in answers.items():

        match = False

        question_config = question_bank.get(key)
        if not question_config:
            continue

        weight = question_config.get("weight", 1)
        total_weight += weight

        data_type = question_config.get("type")
        real_values = real_data.get(data_type)

        if not user_answer or real_values is None:
            analysis[key] = {
                "answer": user_answer,
                "matched": False,
                "weight": weight
            }
            continue

        user_clean = str(user_answer).lower().strip()

        
        if not isinstance(real_values, list):
            real_values = [real_values]

        for value in real_values:
            value_clean = str(value).lower()

            
            if user_clean in value_clean:
                match = True
                break

            
            user_words = user_clean.split()

            for word in user_words:
                if len(word) > 3 and word in value_clean:
                    match = True
                    break

            if match:
                break

        if match:
            score += weight

        analysis[key] = {
            "answer": user_answer,
            "matched": match,
            "weight": weight
        }

    if total_weight == 0:
        return 0, analysis

    final_score = int((score / total_weight) * 100)

    return final_score, analysis


def evaluate_risk(score: int) -> str:
    """
    Determina acción según score acumulado.
    """

    if score >= 70:
        return "approved"

    if 40 <= score < 70:
        return "additional_question"

    return "escalate"