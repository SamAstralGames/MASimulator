import sys


def lancer():
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication

    # Doit précéder la création de QApplication quand QtWebEngine est utilisé.
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)

    from .window import FenetrePrincipale
    fenetre = FenetrePrincipale()
    fenetre.show()
    return app.exec()
