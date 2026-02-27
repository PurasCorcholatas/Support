from langchain_core.tools import tool



@tool
def guie_pw(str): 
    """
    Devuelve la guia paso a paso para cambiar la contraseña de correo
    Se usa cuando el usuario pregunta por:
    
    -Guia para cambio de contraseña de correo
    -Guia para cambio de clave de correo
    -Guia de correo 
    
    """
    
    return"""
    GUIA PARA CAMBIAR SU CONTRASEÑA DE CORREO
1.Ingrese


Si desea puedo generar una contraseña segura que cumpla con los parametros,Solo indicame y puedo generarla.
    """